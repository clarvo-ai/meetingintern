"""Verify that frequent checks cannot dispatch or work a meeting twice."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from archive_checker import check_once, dispatch_analysis_jobs
from archive_job import work_once as archive_work_once
from analysis_state import AnalysisState
from archive_state import ArchiveState
from audio_archive import FOLDER_MIME, Meeting


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
             patch("archive_checker.list_children", return_value=[
                 {"id": "folder", "name": "meeting", "mimeType": FOLDER_MIME}
             ]), \
             patch("archive_checker.find_meetings", return_value=[meeting]):
            result = check_once(["root-one", "root-two"], "destination", "job", state, None,
                                launch=launch)
        self.assertEqual(result["dispatched_count"], 1)
        launch.assert_called_once_with("job", "key")

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
