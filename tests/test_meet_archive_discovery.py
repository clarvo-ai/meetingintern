"""Meet API file IDs must work without shared parent folders."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from meet_archive_discovery import find_meet_meetings, meeting_from_record


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

    def test_approved_additional_file_owner_does_not_expand_meet_scan(self):
        self.doc["owners"] = [{"emailAddress": "juho@clarvo.ai"}]
        self.video["owners"] = [{"emailAddress": "juho@clarvo.ai"}]
        self.record["startTime"] = stamp(timedelta(hours=4))
        self.meet.conferenceRecords.return_value.list.return_value.execute.return_value = {
            "conferenceRecords": [self.record]}
        meet_factory = Mock(return_value=self.meet)
        meetings = list(find_meet_meetings(
            meet_factory, lambda _owner: self.drive, ["robert@clarvo.ai"],
            now=NOW, file_owners=["robert@clarvo.ai", "juho@clarvo.ai"]))
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].source_owner_email, "juho@clarvo.ai")
        meet_factory.assert_called_once_with("robert@clarvo.ai")

    def test_visible_recording_blocks_doc_fallback_until_settled(self):
        self.video["modifiedTime"] = stamp(timedelta(minutes=1))
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))

    def test_ended_recording_falls_back_to_doc_after_three_hours(self):
        self.meet.conferenceRecords.return_value.recordings.return_value.list.return_value.execute.return_value = {
            "recordings": [{"state": "ENDED"}]}
        with patch("meet_archive_discovery.export_transcript", return_value="Hello"):
            meeting = meeting_from_record(self.record, self.meet,
                                          lambda _owner: self.drive, OWNERS, now=NOW)
        self.assertIsNone(meeting.recording_id)

    def test_ended_transcript_falls_back_to_recording_after_three_hours(self):
        self.meet.conferenceRecords.return_value.transcripts.return_value.list.return_value.execute.return_value = {
            "transcripts": [{"state": "ENDED"}]}
        meeting = meeting_from_record(self.record, self.meet,
                                      lambda _owner: self.drive, OWNERS, now=NOW)
        self.assertIsNone(meeting.notes_id)
        self.assertEqual(meeting.recording_id, "video")

    def test_recording_without_transcript_becomes_ready_after_three_hours(self):
        self.meet.conferenceRecords.return_value.transcripts.return_value.list.return_value.execute.return_value = {}
        meeting = meeting_from_record(self.record, self.meet,
                                      lambda _owner: self.drive, OWNERS,
                                      preferred=["robert@clarvo.ai"], now=NOW)
        self.assertIsNone(meeting.notes_id)
        self.assertEqual(meeting.recording_id, "video")
        self.assertEqual(meeting.source_owner_email, "robert@clarvo.ai")
        self.video["createdTime"] = stamp(timedelta(hours=2))
        self.assertIsNone(meeting_from_record(self.record, self.meet,
                                              lambda _owner: self.drive, OWNERS,
                                              now=NOW))


if __name__ == "__main__":
    unittest.main()
