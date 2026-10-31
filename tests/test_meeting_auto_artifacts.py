"""Guard premeeting recording changes against unrelated Calendar events."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from meeting_auto_artifacts import (ARTIFACT_FIELDS, calendar_ids,
                                    enable_upcoming_artifacts)


NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)


def event(identifier, organizer="mark@clarvo.ai", creator="mark@clarvo.ai",
          code="abc-defg-hij", start=None, status="confirmed"):
    return {
        "id": identifier,
        "status": status,
        "start": {"dateTime": (start or NOW + timedelta(hours=1)).isoformat()},
        "organizer": {"email": organizer},
        "creator": {"email": creator},
        "conferenceData": {
            "conferenceId": code,
            "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
        },
    }


def calendar_with(events):
    calendar = Mock()
    calendar.events.return_value.list.return_value.execute.return_value = {"items": events}
    return calendar


class AutoRecordingTests(unittest.TestCase):
    def test_calendar_ids_are_an_explicit_unique_allowlist(self):
        self.assertEqual(calendar_ids(" Mark@Clarvo.ai , juha@clarvo.ai "),
                         ["mark@clarvo.ai", "juha@clarvo.ai"])
        with self.assertRaises(ValueError):
            calendar_ids("mark@clarvo.ai,MARK@clarvo.ai")
        self.assertEqual(calendar_ids("", allow_empty=True), [])
        with self.assertRaises(ValueError):
            calendar_ids("mark@clarvo.ai,MARK@clarvo.ai", allow_empty=True)

    def test_only_future_self_organized_meet_space_is_patched_once(self):
        events = [
            event("eligible"),
            event("same-space", start=NOW + timedelta(hours=2)),
            event("invitation", organizer="outside@example.com"),
            event("created-by-someone-else", creator="outside@example.com"),
            event("already-started", start=NOW - timedelta(minutes=1)),
            event("cancelled", status="cancelled"),
            event("not-a-meet", code="invalid"),
        ]
        calendar = calendar_with(events)
        meet = Mock()
        meet.spaces.return_value.get.return_value.execute.return_value = {
            "name": "spaces/canonical-id", "config": {"artifactConfig": {
                "recordingConfig": {"autoRecordingGeneration": "OFF"}
            }}
        }
        result = enable_upcoming_artifacts(calendar, lambda owner: meet,
                                           ["mark@clarvo.ai"], now=NOW)
        self.assertEqual(result, {"eligible_events": 1, "already_on": 0,
                                  "updated": 1, "errors": 0})
        meet.spaces.return_value.get.assert_called_once_with(name="spaces/abc-defg-hij")
        patch = meet.spaces.return_value.patch
        patch.assert_called_once()
        self.assertEqual(patch.call_args.kwargs["name"], "spaces/canonical-id")
        self.assertEqual(set(patch.call_args.kwargs["updateMask"].split(",")),
                         {f"config.artifactConfig.{config}.{field}"
                          for config, field in ARTIFACT_FIELDS})
        body = patch.call_args.kwargs["body"]["config"]["artifactConfig"]
        self.assertTrue(all(body[config][field] == "ON"
                            for config, field in ARTIFACT_FIELDS))

    def test_existing_artifacts_are_left_alone(self):
        calendar = calendar_with([event("already-on")])
        meet = Mock()
        meet.spaces.return_value.get.return_value.execute.return_value = {
            "name": "spaces/canonical-id", "config": {"artifactConfig": {
                "recordingConfig": {"autoRecordingGeneration": "ON"},
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
            }}
        }
        result = enable_upcoming_artifacts(calendar, lambda owner: meet,
                                           ["mark@clarvo.ai"], now=NOW)
        self.assertEqual(result["already_on"], 1)
        self.assertEqual(result["updated"], 0)
        meet.spaces.return_value.patch.assert_not_called()

    def test_current_organizer_can_enable_event_created_under_old_alias(self):
        calendar = calendar_with([event(
            "weekly-recap", organizer="mark@clarvo.ai", creator="mark@old.example",
            code="klm-nopq-rst")])
        meet = Mock()
        meet.spaces.return_value.get.return_value.execute.return_value = {
            "name": "spaces/recap", "config": {"artifactConfig": {}}}
        result = enable_upcoming_artifacts(calendar, lambda owner: meet,
                                           ["mark@clarvo.ai"], now=NOW)
        self.assertEqual(result["updated"], 1)
        meet.spaces.return_value.get.assert_called_once_with(name="spaces/klm-nopq-rst")

    def test_existing_transcript_and_notes_only_patch_recording(self):
        calendar = calendar_with([event("juha-test")])
        meet = Mock()
        meet.spaces.return_value.get.return_value.execute.return_value = {
            "name": "spaces/canonical-id", "config": {"artifactConfig": {
                "recordingConfig": {"autoRecordingGeneration": "OFF"},
                "transcriptionConfig": {"autoTranscriptionGeneration": "ON"},
                "smartNotesConfig": {"autoSmartNotesGeneration": "ON"},
            }}
        }
        result = enable_upcoming_artifacts(calendar, lambda owner: meet,
                                           ["mark@clarvo.ai"], now=NOW)
        self.assertEqual(result["updated"], 1)
        patch = meet.spaces.return_value.patch
        self.assertEqual(patch.call_args.kwargs["updateMask"],
                         "config.artifactConfig.recordingConfig.autoRecordingGeneration")
        self.assertEqual(patch.call_args.kwargs["body"]["config"]["artifactConfig"],
                         {"recordingConfig": {"autoRecordingGeneration": "ON"}})

    def test_permission_failure_is_reported_for_scheduler_retry(self):
        calendar = calendar_with([event("needs-recording")])
        meet = Mock()
        meet.spaces.return_value.get.return_value.execute.side_effect = PermissionError()
        result = enable_upcoming_artifacts(calendar, lambda owner: meet,
                                           ["mark@clarvo.ai"], now=NOW)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["updated"], 0)


if __name__ == "__main__":
    unittest.main()
