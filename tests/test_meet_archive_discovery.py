"""Meet API file IDs must work without shared parent folders."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from meet_archive_discovery import meeting_from_record


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
OWNERS = ["juha@clarvo.ai", "robert@clarvo.ai", "mark@clarvo.ai"]


def stamp(delta):
    return (NOW - delta).isoformat()


def source_file(identifier, kind, owner, parent):
    return {
        "id": identifier, "name": f"Demo - 2026/10/29 09:00 EET - {kind}",
        "mimeType": ("video/mp4" if kind == "Recording"
                     else "application/vnd.google-apps.document"),
        "createdTime": stamp(timedelta(hours=4)),
        "modifiedTime": stamp(timedelta(minutes=10)),
        "parents": [parent], "owners": [{"emailAddress": owner}],
    }


class MeetDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.record = {"name": "conferenceRecords/one", "endTime": stamp(timedelta(hours=3))}
        self.meet = Mock()
        resource = self.meet.conferenceRecords.return_value
        resource.transcripts.return_value.list.return_value.execute.return_value = {
            "transcripts": [{"state": "FILE_GENERATED",
                             "docsDestination": {"document": "doc"}}]}
        resource.recordings.return_value.list.return_value.execute.return_value = {
            "recordings": [{"state": "FILE_GENERATED",
                            "driveDestination": {"file": "video"}}]}
        self.doc = source_file("doc", "Transcript", "robert@clarvo.ai", "notes-folder")
        self.video = source_file("video", "Recording", "robert@clarvo.ai", "other-folder")
        self.video["videoMediaMetadata"] = {"durationMillis": "2400000"}
        self.drive = Mock()
        self.drive.files.return_value.get.return_value.execute.side_effect = (
            lambda: self.doc if self.drive.files.return_value.get.call_args.kwargs["fileId"] == "doc"
            else self.video)

    def test_pairs_exact_artifact_ids_across_unrelated_folders(self):
        meeting = meeting_from_record(self.record, self.meet,
                                      lambda _owner: self.drive, OWNERS,
                                      preferred=["robert@clarvo.ai"], now=NOW)
        self.assertEqual((meeting.notes_id, meeting.recording_id), ("doc", "video"))
        self.assertEqual((meeting.folder_id, meeting.recording_folder_id),
                         ("notes-folder", "other-folder"))
        self.assertEqual(meeting.source_owner_email, "robert@clarvo.ai")
        self.assertEqual(meeting.source_conference_record, "conferenceRecords/one")

    def test_waits_three_hours_only_when_api_has_no_recording(self):
        self.meet.conferenceRecords.return_value.recordings.return_value.list.return_value.execute.return_value = {}
        self.doc["createdTime"] = stamp(timedelta(hours=2))
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))
        self.doc["createdTime"] = stamp(timedelta(hours=4))
        with patch("meet_archive_discovery.export_transcript", return_value="Hello"):
            meeting = meeting_from_record(self.record, self.meet,
                                          lambda _owner: self.drive, OWNERS, now=NOW)
        self.assertIsNone(meeting.recording_id)

    def test_rejects_external_owned_doc_even_when_invitee_can_read_it(self):
        self.doc["owners"] = [{"emailAddress": "outside@example.com"}]
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))

    def test_visible_recording_blocks_doc_fallback_until_settled(self):
        self.video["modifiedTime"] = stamp(timedelta(minutes=1))
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))

    def test_unfinished_recording_blocks_doc_fallback_after_three_hours(self):
        self.meet.conferenceRecords.return_value.recordings.return_value.list.return_value.execute.return_value = {
            "recordings": [{"state": "ENDED"}]}
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))


if __name__ == "__main__":
    unittest.main()
