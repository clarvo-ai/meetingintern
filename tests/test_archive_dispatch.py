"""Verify that frequent checks cannot dispatch or work a meeting twice."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from archive_checker import check_once, dispatch_analysis_jobs, source_groups
from archive_job import work_once as archive_work_once
from analysis_state import AnalysisState
from archive_state import ArchiveState
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
             patch("archive_job.process_meeting", return_value="output-doc") as process:
            result = archive_work_once("old-key", "destination", "key", state, Mock())
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
