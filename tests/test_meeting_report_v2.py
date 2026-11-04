"""Local full-transcript report iteration checks."""

import unittest
from unittest.mock import patch

from meeting_report_v2 import (MeetingNarrative, NextStepLedger, QuestionLedger,
                               _clean_report, format_report_brief,
                               format_report_chat, generate_report)


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
            generate_report("unused", transcript, evidence, {}, spelling_hints=["ExampleTool"])
        self.assertEqual(len(calls), 5)
        for prompt, _ in calls:
            self.assertIn("Beginning point.", prompt)
            self.assertIn("Ending commitment.", prompt)
            self.assertNotIn("Title with unverified name", prompt)
            self.assertNotIn("https://example.test", prompt)
            self.assertIn("ExampleTool", prompt)
            self.assertIn("hints only", prompt)

    def test_internal_joiners_do_not_prevent_question_extraction(self):
        evidence = {"speakers": ["Juha Lehto", "Robert Holmberg"],
                    "confirmed_meet_participants": [
                        {"display_name": "Juha Lehto", "identity_kind": "signed_in"},
                        {"display_name": "Robert Holmberg", "identity_kind": "signed_in"}]}
        schemas = []

        def fake_generate(_client, _model, _prompt, schema, _limit):
            schemas.append(schema)
            if schema is QuestionLedger:
                return QuestionLedger()
            if schema is NextStepLedger:
                return NextStepLedger()
            return MeetingNarrative(headline="Team update", meeting_type="team_internal",
                                    classification_confidence="high", overview=[])

        with patch("meeting_report_v2._generate_structured", side_effect=fake_generate):
            payload = generate_report("unused", "Juha Lehto: Let's review.\n"
                                      "Robert Holmberg: Agreed.", evidence,
                                      {"juha@clarvo.ai": "Juha Lehto",
                                       "robert@clarvo.ai": "Robert Holmberg"})
        self.assertIn(QuestionLedger, schemas)
        self.assertEqual(payload["questions_and_answers"], [])

    def test_confirmed_internal_meeting_omits_customer_qa(self):
        from meeting_report_v2 import QuestionAndAnswer
        evidence = {"speakers": ["Juha Lehto", "Robert Holmberg"],
                    "archived_speech_words": 60,
                    "confirmed_meet_participants": [
                        {"display_name": "Juha Lehto", "identity_kind": "signed_in"},
                        {"display_name": "Robert Holmberg", "identity_kind": "signed_in"}]}
        question = QuestionAndAnswer(topic="Status", asked_or_needed="How is it going?",
                                     asked_by="clarvo", answer="Well", resolution="answered")

        def fake_generate(_client, _model, _prompt, schema, _limit):
            if schema is QuestionLedger:
                return QuestionLedger(questions_and_answers=[question])
            if schema is NextStepLedger:
                return NextStepLedger()
            return MeetingNarrative(headline="Team update", meeting_type="team_internal",
                                    classification_confidence="high", overview=[])

        with patch("meeting_report_v2._generate_structured", side_effect=fake_generate):
            payload = generate_report("unused", "Juha Lehto: How is it going?\n"
                                      "Robert Holmberg: Well.", evidence,
                                      {"juha@clarvo.ai": "Juha Lehto",
                                       "robert@clarvo.ai": "Robert Holmberg"})
        self.assertEqual(payload["meeting_type"], "team_internal")
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

    def test_both_question_directions_and_problems_reach_chat_and_brief(self):
        payload = {"headline": "Demo", "meeting_type": "unknown",
                   "classification_confidence": "low", "meeting_evidence": {},
                   "overview": [], "discussion_sections": [], "next_steps": [],
                   "problems": ["The CRM contains duplicate company records.",
                                "Mark cannot connect to the virtual machine."],
                   "questions_and_answers": [
                       {"topic": "Hiring", "asked_by": "clarvo",
                        "questioner": "Robert", "asked_or_needed": "How is hiring managed?",
                        "answer": "The team uses an ATS.", "respondent": "Alex",
                        "resolution": "answered"},
                       {"topic": "Filters", "asked_by": "other",
                        "asked_or_needed": "Can results be filtered?",
                        "answer": "Clarvo showed manual filters.",
                        "resolution": "partly_answered"},
                       {"topic": "Pricing", "asked_by": "clarvo",
                        "asked_or_needed": "What budget is available?",
                        "answer": None, "resolution": "open"}]}
        for text in (format_report_chat(payload, "full-doc", "brief-doc"),
                     format_report_brief(payload, "full-doc")):
            self.assertIn("*⚠️ Problems*\n• The CRM contains duplicate company records.", text)
            self.assertIn("Robert asked: How is hiring managed?\n  ↳ Alex: The team uses an ATS.", text)
            self.assertIn("Can results be filtered?\n  ↳ Answer: Clarvo showed manual filters.", text)
            self.assertIn("Clarvo asked: What budget is available?\n  ↳ No clear answer captured [open]", text)
            self.assertNotIn("seller", text.lower())

    def test_next_steps_use_readable_numbered_actions_and_only_flag_uncertain_statuses(self):
        payload = {"headline": "Team update", "meeting_type": "unknown",
                   "classification_confidence": "low", "meeting_evidence": {},
                   "overview": [], "discussion_sections": [], "questions_and_answers": [],
                   "next_steps": [
                       {"action": "Merge duplicate CRM records", "owner": "Juha",
                        "status": "agreed", "timing": "by Friday",
                        "why": "Keep the sales pipeline accurate."},
                       {"action": "Test the connection", "owner": "Juha",
                        "status": "proposed", "timing": None, "why": None},
                       {"action": "Check the phone setup", "owner": None,
                        "status": "unclear", "timing": None, "why": None}]}
        text = format_report_chat(payload, "doc", None)
        self.assertIn("*Juha*\n1. *Merge duplicate CRM records* (by Friday)\n"
                      "   Keep the sales pipeline accurate.\n\n2. *Test the connection* (proposed)", text)
        self.assertIn("*Owner to confirm*\n3. *Check the phone setup* (status unclear)", text)
        self.assertNotIn("Appeared agreed", text)
        self.assertNotIn(";", text)
        agreed = {**payload, "next_steps": [payload["next_steps"][0]]}
        self.assertIn("*📌 Agreed next steps — to verify*", format_report_chat(agreed, "doc", None))

    def test_unverified_question_speaker_names_are_removed(self):
        from meeting_report_v2 import MeetingReport, QuestionAndAnswer
        report = MeetingReport(headline="Demo", meeting_type="unknown",
                               classification_confidence="low", overview=[],
                               questions_and_answers=[QuestionAndAnswer(
                                   topic="Workflow", asked_or_needed="How is it managed?",
                                   asked_by="clarvo", questioner="Robert", respondent="Alex",
                                   answer="With an ATS.", resolution="answered")])
        cleaned = _clean_report(report, "Robert Holmberg: How is it managed?",
                                {"speakers": ["Robert Holmberg"]})
        self.assertEqual(cleaned.questions_and_answers[0].questioner, "Robert Holmberg")
        self.assertIsNone(cleaned.questions_and_answers[0].respondent)

    def test_original_meet_speaker_names_do_not_attribute_unlabelled_audio(self):
        from meeting_report_v2 import DiscussedNextStep, MeetingReport, QuestionAndAnswer
        report = MeetingReport(headline="Demo", meeting_type="unknown",
                               classification_confidence="low", overview=[],
                               next_steps=[DiscussedNextStep(action="Follow up", owner="Robert Holmberg",
                                                            raised_by="Robert Holmberg", status="agreed")],
                               questions_and_answers=[QuestionAndAnswer(
                                   topic="Workflow", asked_or_needed="How is it managed?",
                                   questioner="Robert Holmberg", respondent="Alex Smith",
                                   answer="With an ATS.", resolution="answered")])
        evidence = {"speakers": ["Robert Holmberg", "Alex Smith"]}
        cleaned = _clean_report(report, "How is it managed? With an ATS.", evidence)
        self.assertIsNone(cleaned.next_steps[0].owner)
        self.assertIsNone(cleaned.next_steps[0].raised_by)
        self.assertIsNone(cleaned.questions_and_answers[0].questioner)
        self.assertIsNone(cleaned.questions_and_answers[0].respondent)

    def test_generated_report_preserves_both_question_directions_and_issues(self):
        from meeting_report_v2 import DiscussedNextStep, QuestionAndAnswer
        transcript = ("Robert Holmberg: How does your team manage hiring?\n"
                      "Alex Smith: We use an ATS, but duplicate records slow us down.\n"
                      "Alex Smith: Can we filter the results?\n"
                      "Robert Holmberg: Yes, with manual filters. I'll send a guide.")
        evidence = {"speakers": ["Robert Holmberg", "Alex Smith"],
                    "confirmed_meet_participants": []}
        questions = QuestionLedger(questions_and_answers=[
            QuestionAndAnswer(topic="Hiring workflow", asked_by="clarvo",
                              questioner="Robert Holmberg", asked_or_needed="How is hiring managed?",
                              respondent="Alex Smith", answer="The team uses an ATS.",
                              resolution="answered"),
            QuestionAndAnswer(topic="Filters", asked_by="other", questioner="Alex Smith",
                              asked_or_needed="Can results be filtered?",
                              respondent="Robert Holmberg", answer="Manual filters are available.",
                              resolution="answered")])
        steps = NextStepLedger(next_steps=[DiscussedNextStep(
            action="Send a guide", owner="Robert Holmberg", status="agreed")])
        narrative = MeetingNarrative(headline="Hiring workflow", meeting_type="unknown",
                                     classification_confidence="low", overview=[],
                                     problems=["Duplicate records slow down hiring."])

        def fake_generate(_client, _model, _prompt, schema, _limit):
            return {QuestionLedger: questions, NextStepLedger: steps,
                    MeetingNarrative: narrative}[schema]

        with patch("meeting_report_v2._generate_structured", side_effect=fake_generate):
            payload = generate_report("unused", transcript, evidence,
                                      {"robert@clarvo.ai": "Robert Holmberg"})
        text = format_report_chat(payload, "transcript", "brief")
        self.assertIn("Robert Holmberg asked: How is hiring managed?", text)
        self.assertIn("↳ Alex Smith: The team uses an ATS.", text)
        self.assertIn("Alex Smith asked: Can results be filtered?", text)
        self.assertIn("↳ Robert Holmberg: Manual filters are available.", text)
        self.assertIn("• Duplicate records slow down hiring.", text)
        self.assertIn("1. *Send a guide*", text)

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
