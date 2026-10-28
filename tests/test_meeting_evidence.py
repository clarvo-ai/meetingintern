"""Evidence gates for meeting type, independent of Gemini self-confidence."""

import unittest
from unittest.mock import Mock

from meeting_evidence import (apply_classification_evidence, archived_speech_body,
                              calendar_context, calendar_invitees, parse_meet_documents,
                              meet_participant_context, parse_team_roster,
                              resolve_speaker_identities)
from meeting_analysis import format_brief, format_chat_message


ROSTER = {
    "juha@clarvo.ai": "Juha Lehto",
    "robert@clarvo.ai": "Robert Holmberg",
    "mark@clarvo.ai": "Mark Szucs",
    "miro@clarvo.ai": "Miro Keimiöniemi",
}
TEAM_TRANSCRIPT = (
    "Meeting Oct 26, 2026 - Transcript\n"
    "00:00:00\n"
    "Robert Holmberg: We should review the profile changes and plan the next release together.\n"
    "Mark Szucs: I can walk through the sales feedback and the questions from this week.\n"
    "Miro Keimiöniemi: I have completed the research data changes and can explain the new flow.\n"
    "Juha Lehto: Let's agree on what we will ship and who will own the follow-up.\n"
    "Robert Holmberg: Thanks, that gives us a clear plan for the rest of the week.\n"
    "Transcription ended after 01:00:00\n"
)
BASE = {
    "meeting_type": "team_internal", "classification_confidence": "high",
    "counterparty": None, "topic_tags": ["other"],
    "summary": ["The team discussed a release."], "decisions": [],
    "agreed_actions": [], "suggested_followups": [], "open_questions": [],
}


class EvidenceTests(unittest.TestCase):
    def test_roster_must_be_explicit_and_complete(self):
        raw = '[{"name":"Juha Lehto","email":"juha@clarvo.ai"}]'
        self.assertEqual(parse_team_roster(raw), {"juha@clarvo.ai": "Juha Lehto"})
        self.assertEqual(parse_team_roster(None), {})
        with self.assertRaises(ValueError):
            parse_team_roster('[{"name":"Juha Lehto"}]')

    def test_ad_hoc_speakers_support_team_without_calendar_invites(self):
        evidence = parse_meet_documents([("Notes by Gemini", TEAM_TRANSCRIPT)])
        evidence["archived_speech_words"] = 65
        evidence.update({"calendar_match": "not_found", "calendar_invitees": []})
        result = apply_classification_evidence(BASE, evidence, ROSTER)
        self.assertEqual(result["meeting_type"], "team_internal")
        self.assertEqual(result["classification_confidence"], "high")
        self.assertEqual(result["topic_tags"], [])
        self.assertEqual(len(evidence["speakers"]), 4)
        self.assertEqual(evidence["attendees_in_meet_doc"], [])

    def test_attendees_heading_and_speakers_are_distinct_signals(self):
        text = ("Daily - Transcript\nAttendees\nJuha Lehto, Mark Szucs, Robert Holmberg\n"
                "Transcript\nRobert Holmberg: Let's start.\nMark Szucs: Hello.\n")
        evidence = parse_meet_documents([("Daily - Transcript", text)])
        self.assertEqual(evidence["attendees_in_meet_doc"],
                         ["Juha Lehto", "Mark Szucs", "Robert Holmberg"])
        self.assertEqual(evidence["speakers"], ["Mark Szucs", "Robert Holmberg"])

    def test_empty_demo_notes_do_not_invent_team_or_topics(self):
        content = ("Demo notes mention jarkko@example.com and Robert Holmberg.\n"
                   "Demo - Transcript\nTranscription ended after 00:00:09\n"
                   "This editable transcript was computer generated and might contain errors.")
        evidence = parse_meet_documents([("Demo - Notes by Gemini", content)])
        evidence["archived_speech_words"] = 0
        evidence.update({"calendar_match": "matched",
                         "calendar_invitees": ["robert@clarvo.ai", "jarkko@example.com"]})
        result = apply_classification_evidence(BASE, evidence, ROSTER)
        self.assertEqual(result["meeting_type"], "unknown")
        self.assertEqual(result["classification_confidence"], "low")
        chat = format_chat_message("Demo", "transcript", "brief", result)
        brief = format_brief("Demo", "transcript", result)
        self.assertNotIn("*Type:*", chat)
        self.assertNotIn("*Topics:*", chat)
        self.assertNotIn("Meeting type:", brief)
        self.assertNotIn("Topic tags:", brief)
        self.assertNotIn("confidence)", chat)

    def test_external_invitee_vetoes_team_label(self):
        evidence = parse_meet_documents([("Notes by Gemini", TEAM_TRANSCRIPT)])
        evidence["archived_speech_words"] = 65
        evidence.update({"calendar_match": "matched", "calendar_invitees": ["guest@example.com"]})
        result = apply_classification_evidence(BASE, evidence, ROSTER)
        self.assertEqual(result["meeting_type"], "unknown")

    def test_external_type_needs_both_high_model_confidence_and_outside_evidence(self):
        evidence = parse_meet_documents([("Notes by Gemini", TEAM_TRANSCRIPT)])
        evidence["archived_speech_words"] = 65
        evidence.update({"calendar_match": "matched", "calendar_invitees": ["guest@example.com"]})
        proposed = {**BASE, "meeting_type": "prospect_sales_demo"}
        self.assertEqual(apply_classification_evidence(proposed, evidence, ROSTER)["meeting_type"],
                         "unknown")
        evidence["speakers"].append("Guest Person")
        high = apply_classification_evidence(proposed, evidence, ROSTER)
        self.assertEqual(high["meeting_type"], "prospect_sales_demo")
        self.assertIn("*Type:* Prospect sales or demo",
                      format_chat_message("Demo", "transcript", "brief", high))
        low = apply_classification_evidence(
            {**proposed, "classification_confidence": "low"}, evidence, ROSTER,
        )
        self.assertEqual(low["meeting_type"], "unknown")
        self.assertNotIn("*Type:*", format_chat_message("Demo", "transcript", "brief", low))

    def test_calendar_requires_exact_attachment_match(self):
        calendar = Mock()
        calendar.events.return_value.list.return_value.execute.return_value = {
            "items": [
                {"attachments": [{"fileId": "different-doc"}],
                 "start": {"dateTime": "2026-10-27T10:00:00Z"},
                 "attendees": [{"email": "guest@example.com"}]},
                {"attachments": [{"fileUrl": "https://docs.google.com/document/d/source-doc/edit"}],
                 "start": {"dateTime": "2026-10-27T10:00:00Z"},
                 "attendees": [{"email": "robert@clarvo.ai"},
                               {"email": "guest@example.com"},
                               {"email": "room@example.com", "resource": True}]},
            ]
        }
        found = calendar_invitees(calendar, ["robert@clarvo.ai"], ["source-doc"],
                                  "2026-10-27T10:01:00Z")
        self.assertEqual(found["calendar_match"], "matched")
        self.assertEqual(found["calendar_invitees"],
                         ["guest@example.com", "robert@clarvo.ai"])
        self.assertEqual(calendar.events.return_value.list.call_args.kwargs["calendarId"],
                         "robert@clarvo.ai")

    def test_code_time_and_owner_match_without_shared_folder(self):
        calendar = Mock()
        calendar.events.return_value.list.return_value.execute.return_value = {"items": [{
            "id": "demo", "iCalUID": "demo@example.com",
            "start": {"dateTime": "2026-10-27T10:00:00+02:00"},
            "organizer": {"email": "robert@clarvo.ai"},
            "conferenceData": {"conferenceId": "mow-qtfh-umn"},
            "attendees": [{"email": "guest@example.com", "responseStatus": "accepted"}],
        }]}
        evidence = {"source_artifacts": [
            {"id": "notes", "meeting_at": "2026-10-27T07:59:00+00:00",
             "owner_emails": ["robert@clarvo.ai"], "meet_codes": []},
            {"id": "recording", "meeting_at": "2026-10-27T07:59:00+00:00",
             "owner_emails": ["robert@clarvo.ai"], "meet_codes": ["mow-qtfh-umn"]},
        ]}
        result = calendar_context(calendar, ["robert@clarvo.ai"], evidence)
        self.assertEqual(result["calendar_match_basis"], "meet_code")
        self.assertEqual(result["calendar_event"]["invitees"][0]["response_status"], "accepted")

    def test_title_only_or_ambiguous_events_do_not_match(self):
        calendar = Mock()
        events = [{
            "id": "one", "iCalUID": "one@example.com", "summary": "Agenttidemo",
            "start": {"dateTime": "2026-10-27T10:00:00+02:00"},
            "organizer": {"email": "robert@clarvo.ai"},
        }]
        calendar.events.return_value.list.return_value.execute.return_value = {"items": events}
        evidence = {"source_artifacts": [{"id": "notes", "meeting_at": "2026-10-27T07:59:00+00:00",
                                         "owner_emails": ["robert@clarvo.ai"], "meet_codes": []}]}
        self.assertEqual(calendar_context(calendar, ["robert@clarvo.ai"], evidence)["calendar_match"],
                         "not_found")
        events[0]["attachments"] = [{"fileId": "notes"}]
        events.append({**events[0], "id": "two", "iCalUID": "two@example.com"})
        self.assertEqual(calendar_context(calendar, ["robert@clarvo.ai"], evidence)["calendar_match"],
                         "ambiguous")

    def test_speaker_resolution_requires_unique_full_name(self):
        evidence = {"speakers": ["Robert Holmberg", "Jarkko", "Guest Person"],
                    "calendar_event": {"invitees": [
                        {"name": "Robert Holmberg", "email": "robert@clarvo.ai"},
                        {"name": "Guest Person", "email": "one@example.com"},
                        {"name": "Guest Person", "email": "two@example.com"},
                        {"name": "Jarkko Dahlström", "email": "jarkko@example.com"},
                    ]}}
        self.assertEqual(resolve_speaker_identities(evidence, ROSTER)["resolved_speakers"],
                         [{"speaker": "Robert Holmberg", "email": "robert@clarvo.ai"}])

    def test_meet_recording_id_selects_main_conference_over_reused_code(self):
        meet = Mock()
        records = [{"name": "conferenceRecords/short", "startTime": "2026-10-26T11:38:00Z"},
                   {"name": "conferenceRecords/main", "startTime": "2026-10-26T10:31:00Z"}]
        meet.conferenceRecords.return_value.list.return_value.execute.return_value = {
            "conferenceRecords": records}
        def recordings(**kwargs):
            file_id = "recording" if kwargs["parent"] == "conferenceRecords/main" else "other"
            return Mock(execute=lambda: {"recordings": [{"driveDestination": {"file": file_id}}]})
        meet.conferenceRecords.return_value.recordings.return_value.list.side_effect = recordings
        meet.conferenceRecords.return_value.participants.return_value.list.return_value.execute.return_value = {
            "participants": [{"signedinUser": {"user": "users/123", "displayName": "Robert Holmberg"},
                              "earliestStartTime": "2026-10-26T10:31:10Z"},
                             {"anonymousUser": {"displayName": "Guest"}}]}
        evidence = {"source_artifacts": [
            {"id": "recording", "mime_type": "video/mp4", "meet_codes": ["vsw-jmey-fkr"],
             "meeting_at": "2026-10-26T10:31:00+00:00", "owner_emails": ["robert@clarvo.ai"]}]}
        found = meet_participant_context(lambda user: meet, evidence, ["robert@clarvo.ai"])
        self.assertEqual(found["meet_conference_record"], "conferenceRecords/main")
        self.assertEqual(found["meet_record_match_basis"], "recording_id")
        self.assertEqual([item["identity_kind"] for item in found["confirmed_meet_participants"]],
                         ["signed_in", "anonymous"])
        self.assertIsNone(found["confirmed_meet_participants"][1]["user_id"])

    def test_meet_requires_unique_time_match_when_recording_has_no_api_link(self):
        meet = Mock()
        meet.conferenceRecords.return_value.list.return_value.execute.return_value = {
            "conferenceRecords": [
                {"name": "conferenceRecords/one", "startTime": "2026-10-26T10:31:00Z"},
                {"name": "conferenceRecords/two", "startTime": "2026-10-26T10:34:00Z"},
            ]}
        meet.conferenceRecords.return_value.recordings.return_value.list.return_value.execute.return_value = {
            "recordings": []}
        evidence = {"source_artifacts": [{"id": "notes", "mime_type": "application/vnd.google-apps.document",
                                         "meet_codes": ["vsw-jmey-fkr"],
                                         "meeting_at": "2026-10-26T10:31:00+00:00",
                                         "owner_emails": ["robert@clarvo.ai"]}]}
        result = meet_participant_context(lambda user: meet, evidence, ["robert@clarvo.ai"])
        self.assertEqual(result["meet_participant_record_status"], "ambiguous")
        self.assertEqual(result["confirmed_meet_participants"], [])

    def test_confirmed_joiners_override_invitees_but_anonymous_name_is_not_identity(self):
        evidence = parse_meet_documents([("Notes by Gemini", TEAM_TRANSCRIPT)])
        evidence["archived_speech_words"] = 65
        evidence["calendar_invitees"] = ["guest@example.com"]
        evidence["confirmed_meet_participants"] = [
            {"display_name": "Robert Holmberg", "identity_kind": "signed_in"},
            {"display_name": "Juha Lehto", "identity_kind": "signed_in"},
        ]
        self.assertEqual(apply_classification_evidence(BASE, evidence, ROSTER)["meeting_type"],
                         "team_internal")
        evidence["confirmed_meet_participants"].append(
            {"display_name": "Guest", "identity_kind": "anonymous"})
        self.assertEqual(apply_classification_evidence(BASE, evidence, ROSTER)["meeting_type"],
                         "unknown")

    def test_brief_separates_calendar_invitees_from_speakers(self):
        evidence = {"calendar_match": "matched", "calendar_event": {
            "title": "Demo", "organizer_email": "robert@clarvo.ai",
            "start": "2026-10-27T10:00:00+02:00", "invitees": [
                {"email": "guest@example.com", "name": None, "response_status": "accepted"},
            ]}, "speakers": ["Robert Holmberg"],
            "attendees_in_meet_doc": ["Guest Person"],
            "confirmed_meet_participants": [],
            "resolved_speakers": [{"speaker": "Robert Holmberg", "email": "robert@clarvo.ai"}]}
        payload = {**BASE, "meeting_type": "unknown", "classification_confidence": "low",
                   "meeting_evidence": evidence}
        brief = format_brief("Demo", "transcript", payload)
        chat = format_chat_message("Demo", "transcript", "brief", payload)
        self.assertIn("Calendar invitees (responses do not prove attendance): guest@example.com — accepted", brief)
        self.assertIn("Named transcript speakers: Robert Holmberg", brief)
        self.assertIn("Explicit Meet Doc attendee list: Guest Person", brief)
        self.assertIn("Unambiguous speaker identities: Robert Holmberg <robert@clarvo.ai>", brief)
        self.assertIn("Calendar invitees (attendance unverified)", chat)
        self.assertNotIn("*Type:*", chat)

    def test_anonymous_speaker_is_not_proof_of_external_meeting(self):
        evidence = parse_meet_documents([("Demo - Transcript",
            "Demo - Transcript\nSpeaker 1: We reviewed the product in detail. "
            "The client may or may not have joined.\n")])
        evidence["archived_speech_words"] = 60
        evidence["calendar_invitees"] = []
        proposed = {**BASE, "meeting_type": "prospect_sales_demo"}
        result = apply_classification_evidence(proposed, evidence, ROSTER)
        self.assertEqual(result["meeting_type"], "unknown")

    def test_archive_header_is_not_spoken_content(self):
        archive = ("Team - English Transcript\n\nSource recording: none\n"
                   "Source notes: doc\nMethod: Meet transcript\n\n"
                   "Team - Transcript\nRobert Holmberg: Hello, everyone.\n")
        self.assertEqual(archived_speech_body(archive),
                         "Team - Transcript\nRobert Holmberg: Hello, everyone.")


if __name__ == "__main__":
    unittest.main()
