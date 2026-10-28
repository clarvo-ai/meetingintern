"""Local full-transcript report iteration checks."""

import unittest
from unittest.mock import patch

from meeting_report_v2 import (MeetingNarrative, NextStepLedger, QuestionLedger,
                               _clean_report, format_report_chat, generate_report)


class ReportV2Tests(unittest.TestCase):
    def test_each_generation_pass_receives_all_spoken_content(self):
        transcript = ("Title with unverified name\nSource recording: https://example.test\n"
                      "\n[Chunk starting 00:00:00]\nBeginning point.\n"
                      "[Chunk starting 00:12:00]\nEnding commitment.")
        evidence = {"speakers": [], "confirmed_meet_participants": []}
        calls = []

        def fake_generate(_client, _model, prompt, schema, _limit):
            calls.append((prompt, schema))
            if schema is QuestionLedger:
                return QuestionLedger()
            if schema is NextStepLedger:
                return NextStepLedger()
            return MeetingNarrative(headline="Discussion", meeting_type="unknown",
                                    classification_confidence="low", overview=[])

        with patch("meeting_report_v2._generate_structured", side_effect=fake_generate):
            generate_report("unused", transcript, evidence, {})
        self.assertEqual(len(calls), 5)
        for prompt, _ in calls:
            self.assertIn("Beginning point.", prompt)
            self.assertIn("Ending commitment.", prompt)
            self.assertNotIn("Title with unverified name", prompt)
            self.assertNotIn("https://example.test", prompt)

    def test_internal_joiners_skip_external_question_extraction(self):
        evidence = {"speakers": ["Juha Lehto", "Robert Holmberg"],
                    "confirmed_meet_participants": [
                        {"display_name": "Juha Lehto", "identity_kind": "signed_in"},
                        {"display_name": "Robert Holmberg", "identity_kind": "signed_in"}]}
        schemas = []

        def fake_generate(_client, _model, _prompt, schema, _limit):
            schemas.append(schema)
            if schema is NextStepLedger:
                return NextStepLedger()
            return MeetingNarrative(headline="Team update", meeting_type="team_internal",
                                    classification_confidence="high", overview=[])

        with patch("meeting_report_v2._generate_structured", side_effect=fake_generate):
            payload = generate_report("unused", "Juha Lehto: Let's review.\n"
                                      "Robert Holmberg: Agreed.", evidence,
                                      {"juha@clarvo.ai": "Juha Lehto",
                                       "robert@clarvo.ai": "Robert Holmberg"})
        self.assertNotIn(QuestionLedger, schemas)
        self.assertEqual(payload["questions_and_answers"], [])

    def test_unsupported_person_owner_is_removed(self):
        report = MeetingNarrative(headline="Test", meeting_type="unknown",
                                  classification_confidence="low", overview=[])
        from meeting_report_v2 import MeetingReport, DiscussedNextStep
        complete = MeetingReport(**report.model_dump(), next_steps=[
            DiscussedNextStep(action="Follow up", owner="Robert Holmberg",
                              raised_by="Robert Holmberg", status="proposed")])
        cleaned = _clean_report(complete, "Let's follow up", {"speakers": []})
        self.assertIsNone(cleaned.next_steps[0].owner)
        self.assertIsNone(cleaned.next_steps[0].raised_by)

    def test_joiner_names_do_not_become_unattributed_headline_names(self):
        from meeting_report_v2 import MeetingReport
        report = MeetingReport(headline="Trial discussion with Alex and Sam",
                               meeting_type="unknown", classification_confidence="low",
                               overview=[])
        evidence = {"speakers": [], "confirmed_meet_participants": [
            {"display_name": "Alex", "identity_kind": "signed_in"}]}
        cleaned = _clean_report(report, "The trial was discussed.", evidence)
        self.assertEqual(cleaned.headline, "Trial discussion")

    def test_chat_pairs_answers_and_orders_links(self):
        payload = {"headline": "Demo", "meeting_type": "unknown",
                   "classification_confidence": "low", "meeting_evidence": {},
                   "overview": [], "discussion_sections": [], "decisions": [],
                   "open_questions": [], "next_steps": [],
                   "questions_and_answers": [{"topic": "Filtering",
                      "asked_or_needed": "Can results be filtered?",
                      "clarvo_response": "Clarvo showed manual filters.",
                      "resolution": "answered", "follow_up": None}]}
        text = format_report_chat(payload, "full-doc", "brief-doc")
        self.assertIn("Can results be filtered?\n  ↳ Clarvo: Clarvo showed manual filters.", text)
        self.assertLess(text.index("/full-doc/"), text.index("/brief-doc/"))
        self.assertNotIn("*Type:*", text)

    def test_calendar_link_follows_transcript_and_header_has_time_topics_people(self):
        payload = {"headline": "Demo", "meeting_type": "prospect_sales_demo",
                   "classification_confidence": "high", "topic_tags": ["sales", "other"],
                   "meeting_evidence": {"type_supported": True,
                       "calendar_match": "matched", "calendar_event": {
                           "start": "2026-10-27T10:00:00+02:00",
                           "event_url": "https://calendar.google.com/event/demo",
                           "invitees": [{"email": "luukas.lavanti@brightlyworks.com",
                                         "response_status": "needsAction"}]},
                       "confirmed_meet_participants": [
                           {"display_name": "Luukas", "identity_kind": "anonymous"}]},
                   "verified_companies": [{"name": "Brightly",
                                          "matched_domains": ["brightlyworks.com"]}],
                   "overview": [], "discussion_sections": [], "decisions": [],
                   "open_questions": [], "next_steps": [], "questions_and_answers": []}
        text = format_report_chat(payload, "full-doc", "brief-doc")
        self.assertIn("Prospect sales or demo · #sales", text)
        self.assertNotIn("#other", text)
        self.assertIn("2026/10/27 10:00 EET", text)
        self.assertIn("Brightly (brightlyworks.com)", text)
        self.assertIn("Luukas Lavanti <luukas.lavanti@brightlyworks.com>", text)
        self.assertLess(text.index("/full-doc/"), text.index("calendar.google.com"))
        self.assertLess(text.index("calendar.google.com"), text.index("/brief-doc/"))


if __name__ == "__main__":
    unittest.main()
