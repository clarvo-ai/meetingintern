"""Verify that frequent checks cannot dispatch or work a meeting twice."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from archive_checker import check_once, dispatch_analysis_jobs, source_groups
from archive_job import work_once as archive_work_once
from analysis_state import AnalysisState
from archive_state import ArchiveState, _verified_meeting_start
from audio_archive import Meeting


class FakeSnapshot:
    def __init__(self, data):
        self.exists = bool(data)
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeReference:
    def __init__(self):
        self.data = {}

    def get(self, transaction=None):
        return FakeSnapshot(self.data)

    def update(self, values):
        self.data.update(values)


class FakeTransaction:
    def set(self, ref, values, merge=False):
        ref.data.update(values)

    def update(self, ref, values):
        ref.data.update(values)


class FakeCollection:
    def __init__(self):
        self.refs = {}

    def document(self, key):
        return self.refs.setdefault(key, FakeReference())


class FakeClient:
    def transaction(self):
        return FakeTransaction()


class DispatchTests(unittest.TestCase):
    def test_claim_blocks_second_checker_and_second_worker(self):
        state = ArchiveState.__new__(ArchiveState)
        state.client = FakeClient()
        state.collection = FakeCollection()
        meeting = SimpleNamespace(source_key="same-meeting", folder_id="folder", notes_id="doc")
        with patch("archive_state.firestore.transactional", lambda function: function):
            self.assertTrue(state.reserve_for_dispatch(meeting, "root"))
            self.assertFalse(state.reserve_for_dispatch(meeting, "root"))
            self.assertIsNotNone(state.claim_for_work(meeting.source_key))
            state.mark_queued(meeting.source_key, "execution")
            self.assertIsNone(state.claim_for_work(meeting.source_key))
            state.mark_done(meeting.source_key, "output-doc")
            self.assertFalse(state.reserve_for_dispatch(meeting, "root"))
            self.assertEqual(state.collection.document(meeting.source_key).data["analysis_status"], "pending")

    def test_late_dispatch_error_cannot_release_running_worker(self):
        state = ArchiveState.__new__(ArchiveState)
        state.client = FakeClient()
        state.collection = FakeCollection()
        meeting = SimpleNamespace(source_key="same-meeting", folder_id="folder", notes_id="doc")
        with patch("archive_state.firestore.transactional", lambda function: function):
            self.assertTrue(state.reserve_for_dispatch(meeting, "root"))
            self.assertIsNotNone(state.claim_for_work(meeting.source_key))
            state.mark_retry(meeting.source_key, "late launch response", "dispatching")
            self.assertEqual(state.collection.document(meeting.source_key).data["status"], "processing")

    def test_first_seen_is_recorded_once_for_new_meetings_only(self):
        state = ArchiveState.__new__(ArchiveState)
        state.client, state.collection = FakeClient(), FakeCollection()
        meeting = SimpleNamespace(source_key="new", folder_id="folder", notes_id="doc")
        first = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        with patch("archive_state.firestore.transactional", lambda function: function), \
             patch("archive_state._utc_now", return_value=first) as clock:
            self.assertTrue(state.reserve_for_dispatch(meeting, "root"))
            ref = state.collection.document("new")
            self.assertEqual(ref.data["archive_first_seen_at"], first)
            self.assertEqual(ref.data["meeting_ready_status"], "awaiting_brief")
            clock.return_value = first.replace(hour=13)
            state.mark_retry("new", "temporary", "dispatching")
            clock.return_value = first.replace(hour=14)
            self.assertTrue(state.reserve_for_dispatch(meeting, "root"))
            self.assertEqual(ref.data["archive_first_seen_at"], first)
            old = state.collection.document("old")
            old.data.update({"status": "pending"})
            meeting.source_key = "old"
            self.assertTrue(state.reserve_for_dispatch(meeting, "root"))
            self.assertNotIn("archive_first_seen_at", old.data)

    def test_cutover_uses_meeting_time_not_later_artifact_creation(self):
        meeting = SimpleNamespace(folder_name="Demo - 2026/11/04 12:30 EET",
                                  verified_started_at=None)
        self.assertEqual(_verified_meeting_start(meeting), datetime(2026, 11, 4, 10, 30, tzinfo=timezone.utc))
        meeting.folder_name = "Ad hoc meeting without a trustworthy time"
        self.assertIsNone(_verified_meeting_start(meeting))
        meeting.verified_started_at = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        self.assertEqual(_verified_meeting_start(meeting), meeting.verified_started_at)

    def test_same_doc_seen_in_two_roots_launches_once(self):
        meeting = Meeting(
            source_key="key", folder_id="folder", folder_name="Test",
            recording_id=None, recording_name=None, duration_seconds=None,
            notes_id="same-doc", notes_name="Transcript", context_doc_ids=("same-doc",),
        )
        state = Mock()
        state.reserve_for_dispatch.return_value = True
        launch = Mock(return_value="execution")
        with patch("archive_checker.find_archived_recordings", return_value=set()), \
             patch("archive_checker.find_meetings_across_roots", return_value=[meeting]):
            result = check_once(["root-one", "root-two"], "destination", "job", state, None,
                                launch=launch)
        self.assertEqual(result["dispatched_count"], 1)
        launch.assert_called_once_with("job", "key")

    def test_exact_meet_discovery_precedes_and_deduplicates_folder_fallback(self):
        direct = Meeting(
            source_key="meet-key", folder_id="unshared-folder", folder_name="Test",
            recording_id="video", recording_name="Recording", duration_seconds=60,
            notes_id="doc", notes_name="Transcript", context_doc_ids=("doc",),
            source_owner_email="robert@clarvo.ai",
            source_conference_record="conferenceRecords/one",
        )
        fallback = Meeting(
            source_key="folder-key", folder_id="shared-folder", folder_name="Test",
            recording_id="video", recording_name="Recording", duration_seconds=60,
            notes_id="doc", notes_name="Transcript", context_doc_ids=("doc",),
        )
        state = Mock()
        state.reserve_for_dispatch.return_value = True
        launch = Mock(return_value="execution")
        with patch("archive_checker.find_archived_recordings", return_value=set()), \
             patch("archive_checker.find_meetings_across_roots", return_value=[fallback]):
            result = check_once(["shared-root"], "destination", "job", state, None,
                                launch=launch, direct_meetings=[direct])
        self.assertEqual(result["dispatched_count"], 1)
        launch.assert_called_once_with("job", "meet-key")

    def test_archive_worker_recovers_when_drive_changes_computed_source_key(self):
        meeting = Meeting(
            source_key="new-key", folder_id="folder", folder_name="Test",
            recording_id=None, recording_name=None, duration_seconds=None,
            notes_id="source-doc", notes_name="Transcript",
            context_doc_ids=("source-doc",),
        )
        state = Mock()
        state.claim_for_work.return_value = {
            "meeting_folder_id": "folder", "source_notes_id": "source-doc",
            "source_context_doc_ids": ["source-doc"],
        }
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {"id": "folder"}
        with patch("archive_job.build", return_value=drive), \
             patch("archive_job.find_meetings", return_value=[meeting]), \
             patch("archive_job.existing_archive_doc_id", return_value=None), \
             patch("archive_job.process_meeting", return_value="output-doc") as process, \
             patch("archive_job.intelligence_log") as logged:
            result = archive_work_once("old-key", "destination", "key", state, Mock())
        logged.emit.assert_called_once_with("meeting_archived", key="old-key")
        self.assertEqual(result, "output-doc")
        process.assert_called_once()
        state.mark_done.assert_called_once_with("old-key", "output-doc")

    def test_archive_worker_rechecks_both_roots_before_processing(self):
        meeting = Meeting("key", "notes-folder", "Demo", "video", "Recording",
                          60, "source-doc", "Notes by Gemini", ("source-doc",),
                          "video-folder")
        state = Mock()
        state.claim_for_work.return_value = {
            "source_root_ids": ["notes-root", "video-root"],
            "source_notes_id": "source-doc", "source_context_doc_ids": ["source-doc"],
        }
        with patch("archive_job.build", return_value=Mock()), \
             patch("archive_job.find_meetings_across_roots", return_value=[meeting]) as discover, \
             patch("archive_job.existing_archive_doc_id", return_value=None), \
             patch("archive_job.process_meeting", return_value="output-doc") as process:
            result = archive_work_once("key", "destination", "key", state, Mock())
        self.assertEqual(result, "output-doc")
        discover.assert_called_once_with(unittest.mock.ANY, ["notes-root", "video-root"])
        process.assert_called_once()

    def test_existing_raw_archive_is_not_accepted_as_clean(self):
        meeting = Meeting("key", "folder", "Demo", None, None, None,
                          "source-doc", "Transcript", ("source-doc",))
        state, drive = Mock(), Mock()
        state.claim_for_work.return_value = {"source_root_ids": ["root"],
                                              "source_notes_id": "source-doc"}
        drive.files.return_value.get.return_value.execute.return_value = {"properties": {}}
        with patch("archive_job.build", return_value=drive), \
             patch("archive_job.find_meetings_across_roots", return_value=[meeting]), \
             patch("archive_job.existing_archive_doc_id", return_value="existing"):
            self.assertIsNone(archive_work_once("key", "destination", "key", state, Mock()))
        state.mark_done.assert_not_called()
        state.mark_retry.assert_called_once_with("key", "Existing transcript requires cleanup backfill", "processing")

    def test_direct_worker_rechecks_meet_record_and_reads_as_source_owner(self):
        meeting = Meeting(
            source_key="meet-key", folder_id="unshared-folder", folder_name="Test",
            recording_id="video", recording_name="Recording", duration_seconds=60,
            notes_id="doc", notes_name="Transcript", context_doc_ids=("doc",),
            source_owner_email="robert@clarvo.ai",
            recording_owner_email="robert@clarvo.ai",
            source_conference_record="conferenceRecords/one",
            meet_reader_email="robert@clarvo.ai",
        )
        state = Mock()
        state.claim_for_work.return_value = {
            "source_conference_record": "conferenceRecords/one",
            "source_owner_email": "robert@clarvo.ai",
            "meet_reader_email": "robert@clarvo.ai",
            "source_notes_id": "doc",
        }
        with patch("archive_job.build", return_value=Mock()), \
             patch("archive_job.meet_client_for_user") as meet_client, \
             patch("archive_job.owner_drive_credentials", return_value=Mock()), \
             patch("archive_job.meeting_from_record", return_value=meeting) as discover, \
             patch("archive_job.existing_archive_doc_id", return_value=None), \
             patch("archive_job.process_meeting", return_value="output-doc") as process:
            result = archive_work_once(
                "meet-key", "destination", "key", state, Mock(),
                "drive-automation@example.com", ["robert@clarvo.ai"])
        self.assertEqual(result, "output-doc")
        meet_client.assert_called_once_with("drive-automation@example.com", "robert@clarvo.ai")
        discover.assert_called_once()
        self.assertIn("source_drive", process.call_args.kwargs)
        self.assertIn("recording_credentials", process.call_args.kwargs)

    def test_cleanup_receives_roster_directory_and_matched_calendar_hints(self):
        meeting = Meeting("meet-key", "folder", "Demo", "video", "Recording",
                          60, "doc", "Transcript", ("doc",),
                          source_owner_email="robert@clarvo.ai",
                          recording_owner_email="robert@clarvo.ai",
                          source_conference_record="conferenceRecords/one",
                          meet_reader_email="robert@clarvo.ai")
        state = Mock()
        state.claim_for_work.return_value = {
            "source_conference_record": "conferenceRecords/one",
            "source_owner_email": "robert@clarvo.ai", "source_notes_id": "doc"}
        roster = {"robert@clarvo.ai": "Robert Holmberg"}
        references = [{"name": "Example", "aliases": ["Example Co"]}]
        with patch("archive_job.build", return_value=Mock()), \
             patch("archive_job.meet_client_for_user"), \
             patch("archive_job.owner_drive_credentials", return_value=Mock()), \
             patch("archive_job.meeting_from_record", return_value=meeting), \
             patch("archive_job.existing_archive_doc_id", return_value=None), \
             patch("archive_job.source_evidence", return_value={"speakers": ["Robert Holmberg"]}), \
             patch("archive_job.calendar_context", return_value={"calendar_match": "matched",
                                                                   "calendar_event": {"title": "Demo"}}), \
             patch("archive_job.process_meeting", return_value="output") as process:
            archive_work_once("meet-key", "destination", "key", state, Mock(),
                              "drive@example.com", ["robert@clarvo.ai"], Mock(), roster, references,
                              ["Codex"])
        context = process.call_args.kwargs["cleanup_context"]
        self.assertEqual(context["team_roster"], ["Robert Holmberg"])
        self.assertEqual(context["company_directory"][0]["name"], "Example")
        self.assertEqual(context["calendar_event"]["title"], "Demo")
        self.assertEqual(context["product_and_tool_names"], ["Codex"])

    def test_root_groups_keep_owners_separate(self):
        self.assertEqual(source_groups(["juha", "robert"]), [["juha"], ["robert"]])
        self.assertEqual(source_groups([], '[["juha"],["robert-a","robert-b"]]'),
                         [["juha"], ["robert-a", "robert-b"]])
        with self.assertRaisesRegex(ValueError, "only one owner group"):
            source_groups([], '[["robert-a"],["robert-a"]]')

    def test_analysis_claim_blocks_duplicate_dispatch_and_worker(self):
        state = AnalysisState.__new__(AnalysisState)
        state.client = FakeClient()
        state.collection = FakeCollection()
        ref = state.collection.document("meeting")
        ref.data.update({"status": "done", "output_doc_id": "transcript",
                         "analysis_status": "pending"})
        with patch("analysis_state.firestore.transactional", lambda function: function):
            self.assertTrue(state.reserve_for_dispatch("meeting"))
            self.assertFalse(state.reserve_for_dispatch("meeting"))
            self.assertIsNotNone(state.claim_for_work("meeting"))
            state.mark_queued("meeting", "execution")
            self.assertIsNone(state.claim_for_work("meeting"))
            state.mark_sending("meeting")
            self.assertFalse(state.reserve_for_dispatch("meeting"))
            self.assertIsNone(state.claim_for_work("meeting"))
            state.mark_posted("meeting", "spaces/space/messages/message")
            self.assertFalse(state.reserve_for_dispatch("meeting"))

    def test_analysis_dispatch_reserves_before_launch(self):
        state = Mock()
        state.candidates.return_value = ["meeting-one", "meeting-two"]
        state.reserve_for_dispatch.side_effect = [True, False]
        launch = Mock(return_value="execution")
        dispatched = dispatch_analysis_jobs(state, "job", launch=launch)
        self.assertEqual(dispatched, ["meeting-one"])
        launch.assert_called_once_with("job", "meeting-one")
        state.mark_queued.assert_called_once_with("meeting-one", "execution")

    def test_analysis_dispatch_waits_when_oldest_meeting_is_active(self):
        state = Mock()
        state.candidates.return_value = ["old", "new"]
        state.reserve_for_dispatch.return_value = False
        launch = Mock()
        self.assertEqual(dispatch_analysis_jobs(state, "job", launch=launch), [])
        state.reserve_for_dispatch.assert_called_once_with("old")
        launch.assert_not_called()

    def test_analysis_candidates_are_sorted_by_meeting_start(self):
        state = AnalysisState.__new__(AnalysisState)
        state.collection = Mock()
        earlier = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
        later = datetime(2026, 10, 29, 15, tzinfo=timezone.utc)
        snapshots = [
            SimpleNamespace(id="late", to_dict=lambda: {"meeting_started_at": later}),
            SimpleNamespace(id="early", to_dict=lambda: {"meeting_started_at": earlier}),
        ]
        state.collection.where.return_value.stream.return_value = snapshots
        self.assertEqual(state.candidates(), ["early", "late"])

    def test_empty_analysis_source_cannot_be_dispatched_again(self):
        state = AnalysisState.__new__(AnalysisState)
        state.client = FakeClient()
        state.collection = FakeCollection()
        ref = state.collection.document("empty-meeting")
        ref.data.update({"status": "done", "output_doc_id": "empty-doc",
                         "analysis_status": "processing"})
        with patch("analysis_state.firestore.transactional", lambda function: function):
            state.mark_invalid_source("empty-meeting", "No spoken words")
            self.assertEqual(ref.data["analysis_status"], "invalid_source")
            self.assertFalse(state.reserve_for_dispatch("empty-meeting"))


if __name__ == "__main__":
    unittest.main()
