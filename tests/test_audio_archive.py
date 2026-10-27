"""Focused tests for matching and waiting on Meet artifacts."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from audio_archive import (AudioGemini, DOC_MIME, RECORDING_MIME, Meeting, duration_exceeds_limit,
                           export_transcript, find_meetings, find_meetings_in_folders,
                           process_meeting)


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

    def test_transcript_only_waits_three_hours_and_keeps_identity_when_video_arrives(self):
        notes = artifact("Test - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "notes", 181)
        folder = {"id": "folder", "name": "Test"}
        with patch("audio_archive.list_children", return_value=[notes]), \
             patch("audio_archive.export_transcript", return_value="Ready transcript"):
            fallback = find_meetings(None, folder)
        self.assertEqual(len(fallback), 1)
        self.assertIsNone(fallback[0].recording_id)

        video = artifact("Test - 2026/10/27 09:00 EET - Recording", RECORDING_MIME, "video")
        with patch("audio_archive.list_children", return_value=[notes, video]):
            recorded = find_meetings(None, folder)
        self.assertEqual(recorded[0].recording_id, "video")
        self.assertEqual(recorded[0].source_key, fallback[0].source_key)

        recent = artifact("Test - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "recent", 179)
        with patch("audio_archive.list_children", return_value=[recent]):
            self.assertEqual(find_meetings(None, folder), [])

        old_created = artifact("Test - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "editing", 190)
        old_created["modifiedTime"] = recent["modifiedTime"]
        with patch("audio_archive.list_children", return_value=[old_created]):
            self.assertEqual(find_meetings(None, folder), [])

        with patch("audio_archive.list_children", return_value=[notes]), \
             patch("audio_archive.export_transcript", return_value=""):
            self.assertEqual(find_meetings(None, folder), [])

    def test_new_recording_blocks_transcript_fallback_until_settled(self):
        notes = artifact("Test - 2026/10/27 09:00 EET - Transcript", DOC_MIME, "notes", 190)
        video = artifact("Test - 2026/10/27 09:00 EET - Recording", RECORDING_MIME, "video", 1)
        with patch("audio_archive.list_children", return_value=[notes, video]):
            self.assertEqual(find_meetings(None, {"id": "folder", "name": "Test"}), [])

    def test_pairs_recording_and_notes_in_different_folders(self):
        notes = artifact("Demo | Robert - 2026/10/27 09:59 EET - Notes by Gemini",
                         DOC_MIME, "notes", 190)
        recording = artifact("Demo | Robert - 2026/10/27 09:59 EET - Recording",
                             RECORDING_MIME, "video", 10)
        recording["videoMediaMetadata"] = {"durationMillis": "2208458"}
        def children(_drive, folder_id):
            return {"notes-folder": [notes], "video-folder": [recording]}[folder_id]
        with patch("audio_archive.list_children", side_effect=children):
            result = find_meetings_in_folders(None, [{"id": "notes-folder"},
                                                      {"id": "video-folder"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].recording_id, "video")
        self.assertEqual(result[0].recording_folder_id, "video-folder")
        self.assertEqual(result[0].folder_id, "notes-folder")

    def test_same_time_unrelated_recording_in_other_folder_does_not_pair(self):
        notes = artifact("Internal planning - 2026/10/27 09:59 EET - Transcript",
                         DOC_MIME, "notes", 181)
        recording = artifact("Customer demo - 2026/10/27 09:59 EET - Recording",
                             RECORDING_MIME, "unrelated-video", 10)
        def children(_drive, folder_id):
            return {"notes-folder": [notes], "video-folder": [recording]}[folder_id]
        with patch("audio_archive.list_children", side_effect=children), \
             patch("audio_archive.export_transcript", return_value="Real speech"):
            result = find_meetings_in_folders(None, [{"id": "notes-folder"},
                                                      {"id": "video-folder"}])
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].recording_id)


class ProcessingTests(unittest.TestCase):
    def meeting(self, recording_id="video"):
        return Meeting("key", "folder", "Test", recording_id, "Recording" if recording_id else None,
                       60 if recording_id else None, "notes", "Transcript", ("notes",))

    def test_empty_meet_transcript_does_not_publish_notes_as_speech(self):
        meeting = replace(self.meeting(None), notes_name="Demo - Notes by Gemini")
        text = (
            "Gemini summarized a product demo here.\n\n"
            "Demo - Transcript\n"
            "Transcription ended after 00:00:09\n"
            "This editable transcript was computer generated and might contain errors."
        )
        drive = Mock()
        drive.files.return_value.export.return_value.execute.return_value = text.encode()
        self.assertEqual(export_transcript(drive, meeting), "")
        spoken = text.replace("Transcription ended after 00:00:09",
                              "00:00:03\nHello, everyone.\nTranscription ended after 00:00:09")
        drive.files.return_value.export.return_value.execute.return_value = spoken.encode()
        self.assertIn("Hello, everyone.", export_transcript(drive, meeting))

    def test_english_recording_uses_meet_transcript(self):
        gemini = Mock()
        gemini.detect_language.return_value = "english"
        with patch("audio_archive.recording_proxy") as proxy, \
             patch("audio_archive.extract_audio_chunks", return_value=["chunk"]), \
             patch("audio_archive.export_transcript", return_value="Meet words"), \
             patch("audio_archive.create_archive_doc", return_value="output") as create:
            proxy.return_value.__enter__.return_value = "recording-url"
            output = process_meeting(None, None, None, gemini, "destination", self.meeting())
        self.assertEqual(output, "output")
        self.assertEqual(create.call_args.args[-2:], ("Meet words", "Google Meet transcript (English)"))
        gemini.translate_chunks.assert_not_called()

    def test_non_english_recording_uses_audio_and_name_hints(self):
        gemini = Mock()
        gemini.detect_language.return_value = "other"
        gemini.translate_chunks.return_value = "Translated speech"
        with patch("audio_archive.recording_proxy") as proxy, \
             patch("audio_archive.extract_audio_chunks", return_value=["chunk"]), \
             patch("audio_archive.person_context", return_value="Juha, juha@clarvo.ai"), \
             patch("audio_archive.export_transcript") as export, \
             patch("audio_archive.create_archive_doc", return_value="output") as create:
            proxy.return_value.__enter__.return_value = "recording-url"
            output = process_meeting(None, None, None, gemini, "destination", self.meeting())
        self.assertEqual(output, "output")
        gemini.translate_chunks.assert_called_once_with(["chunk"], "Juha, juha@clarvo.ai")
        export.assert_not_called()
        self.assertEqual(create.call_args.args[-2:],
                         ("Translated speech", "Gemini audio transcription and English translation"))

    def test_missing_recording_uses_transcript_without_audio(self):
        gemini = Mock()
        with patch("audio_archive.export_transcript", return_value="Meet words"), \
             patch("audio_archive.create_archive_doc", return_value="output") as create:
            output = process_meeting(None, None, None, gemini, "destination", self.meeting(None))
        self.assertEqual(output, "output")
        self.assertIn("recording unavailable after three hours", create.call_args.args[-1])
        gemini.detect_language.assert_not_called()

    def test_finnish_audio_output_is_translated_before_archive(self):
        gemini = AudioGemini.__new__(AudioGemini)
        gemini._with_uploaded_audio = Mock(return_value="Tämä on suomenkielinen puhe.")
        gemini._ask_text = Mock(side_effect=["OTHER", "This is Finnish speech.", "ENGLISH"])
        result = gemini.translate_chunks(["chunk"])
        self.assertIn("This is Finnish speech.", result)
        self.assertNotIn("Tämä", result)

    def test_untranslated_audio_response_fails_closed(self):
        gemini = AudioGemini.__new__(AudioGemini)
        gemini._with_uploaded_audio = Mock(return_value="Tämä on suomenkielinen puhe.")
        gemini._ask_text = Mock(side_effect=["OTHER", "Tämä on edelleen suomea.", "OTHER"])
        with self.assertRaisesRegex(RuntimeError, "did not return an English transcript"):
            gemini.translate_chunks(["chunk"])

    def test_silent_audio_chunk_does_not_create_invented_speech(self):
        gemini = AudioGemini.__new__(AudioGemini)
        gemini._with_uploaded_audio = Mock(return_value="NO_SPEECH")
        gemini._ask_text = Mock()
        self.assertEqual(gemini.translate_chunks(["chunk"]), "")
        gemini._ask_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
