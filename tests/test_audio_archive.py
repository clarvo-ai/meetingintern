"""Focused tests for matching and waiting on Meet artifacts."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from audio_archive import DOC_MIME, RECORDING_MIME, duration_exceeds_limit, find_meetings


def artifact(name, mime_type, identifier, changed_minutes_ago=30):
    changed = (datetime.now(timezone.utc) - timedelta(minutes=changed_minutes_ago)).isoformat()
    return {
        "id": identifier,
        "name": name,
        "mimeType": mime_type,
        "size": "1000",
        "createdTime": changed,
        "modifiedTime": changed,
    }


class MeetingDiscoveryTests(unittest.TestCase):
    def test_eight_hour_boundary_is_inclusive(self):
        self.assertFalse(duration_exceeds_limit(8 * 60 * 60))
        self.assertTrue(duration_exceeds_limit(8 * 60 * 60 + 0.001))
        self.assertFalse(duration_exceeds_limit(None))

    def test_seven_and_half_hour_recording_is_eligible_regardless_of_size(self):
        recording = artifact("Long - 2026/10/27 09:00 EET - Recording", RECORDING_MIME, "video")
        recording["size"] = str(12 * 1024 * 1024 * 1024)
        recording["videoMediaMetadata"] = {"durationMillis": str(int(7.5 * 60 * 60 * 1000))}
        notes = artifact("Long - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "notes")
        with patch("audio_archive.list_children", return_value=[recording, notes]):
            meetings = find_meetings(None, {"id": "folder", "name": "Long"})
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0].duration_seconds, 7.5 * 60 * 60)
        self.assertFalse(duration_exceeds_limit(meetings[0].duration_seconds))

    def test_pairs_recurring_meetings_by_occurrence_not_folder(self):
        children = [
            artifact("Daily - 2026/10/27 09:00 EET - Recording", RECORDING_MIME, "video-one"),
            artifact("Daily - 2026/10/28 09:00 EET - Recording", RECORDING_MIME, "video-two"),
            artifact("Daily - 2026/10/28 09:00 EET - Notes by Gemini", DOC_MIME, "notes-two"),
            artifact("Daily - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "notes-one"),
        ]
        with patch("audio_archive.list_children", return_value=children):
            meetings = find_meetings(None, {"id": "folder", "name": "Daily recurring"})
        self.assertEqual(
            {meeting.recording_id: meeting.notes_id for meeting in meetings},
            {"video-one": "notes-one", "video-two": "notes-two"},
        )

    def test_waits_for_notes_and_video_to_settle(self):
        children = [
            artifact("Test - 2026/10/27 09:00 EET - Recording", RECORDING_MIME, "video"),
            artifact("Test - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "notes", 1),
        ]
        with patch("audio_archive.list_children", return_value=children):
            self.assertEqual(find_meetings(None, {"id": "folder", "name": "Test"}), [])


if __name__ == "__main__":
    unittest.main()
