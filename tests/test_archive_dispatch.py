"""Verify that frequent checks cannot dispatch or work a meeting twice."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from archive_checker import check_once
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


if __name__ == "__main__":
    unittest.main()
