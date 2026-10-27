"""Evidence gates for meeting type, independent of Gemini self-confidence."""

import unittest
from unittest.mock import Mock

from meeting_evidence import (apply_classification_evidence, archived_speech_body,
                              calendar_invitees, parse_meet_documents, parse_team_roster)
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
                 "attendees": [{"email": "guest@example.com"}]},
                {"attachments": [{"fileUrl": "https://docs.google.com/document/d/source-doc/edit"}],
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
