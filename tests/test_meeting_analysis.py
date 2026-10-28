"""Focused checks for the brief and Chat delivery contract."""

import unittest
from unittest.mock import Mock, patch

from meeting_analysis import (EvidenceInventory, EvidencePoint, MAX_INVENTORY_CHARS,
                              MeetingBrief, _extract_evidence_inventory,
                              _ground_customer_sections, _prefer_company_spelling,
                              format_brief, format_chat_message, post_chat_message)
from analysis_job import work_once


BRIEF = {
    "meeting_type": "team_internal",
    "classification_confidence": "high",
    "meeting_evidence": {"type_supported": True, "speakers": ["Juha Lehto", "Robert Holmberg"]},
    "counterparty": None,
    "topic_tags": ["sales", "product_development"],
    "summary": ["The team reviewed the launch plan."],
    "decisions": ["Launch on Friday."],
    "agreed_actions": [{"task": "Prepare the demo", "owner": "Juha",
                        "due_date": "Friday", "evidence": "Juha will prepare the demo"}],
    "suggested_followups": ["Consider a customer survey."],
    "open_questions": ["Who will host the webinar?"],
}


class BriefTests(unittest.TestCase):
    def test_brief_separates_agreed_actions_from_ideas_and_links_transcript(self):
        text = format_brief("Team planning", "source-doc", BRIEF)
        self.assertIn("Agreed actions\n• Prepare the demo (Owner: Juha; Due: Friday)", text)
        self.assertIn("Suggested follow-ups (not agreed commitments)\n• Consider a customer survey", text)
        self.assertIn("https://docs.google.com/document/d/source-doc/edit", text)
        chat = format_chat_message("Team planning", "source-doc", "brief-doc", BRIEF)
        self.assertIn("*Type:* Team meeting", chat)
        self.assertIn("#sales", chat)
        self.assertIn("*Suggested follow-ups (not agreed)*", chat)

    def test_webhook_rejects_general_space_without_sending(self):
        with patch("meeting_analysis.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "configured Meeting Summaries space"):
                post_chat_message(
                    "https://chat.googleapis.com/v1/spaces/GENERAL/messages?key=k&token=t",
                    "MEETINGS", "source-key", "brief text",
                )
        post.assert_not_called()

    def test_legacy_high_confidence_without_evidence_does_not_show_type(self):
        payload = {**BRIEF, "meeting_evidence": None, "topic_tags": ["other"]}
        text = format_chat_message("Unknown meeting", "source", "brief", payload)
        self.assertNotIn("*Type:*", text)
        self.assertNotIn("*Topics:*", text)

    def test_customer_details_and_seller_claims_are_separate_in_both_outputs(self):
        payload = {
            **BRIEF,
            "meeting_type": "unknown", "classification_confidence": "low",
            "meeting_evidence": {"type_supported": False},
            "customer_needs": ["The customer recruits across two teams."],
            "buying_considerations": ["They asked whether an existing tool is required."],
            "seller_claims": ["The seller offered a trial."],
            "commercial_status": "Evaluation agreed; no purchase decision recorded.",
        }
        brief = format_brief("Customer conversation", "source", payload)
        chat = format_chat_message("Customer conversation", "source", "brief", payload)
        for section in ("Customer needs and context", "Customer questions and buying considerations",
                        "Seller claims and offers", "Commercial status"):
            self.assertIn(section, brief)
            self.assertIn(section, chat)
        self.assertNotIn("*Type:*", chat)
        self.assertLess(chat.index("The customer recruits"), chat.index("The seller offered"))

    def test_known_brand_spelling_is_corrected_without_changing_evidence_quotes(self):
        payload = {
            **BRIEF,
            "summary": ["The seller showed Klarvo."],
            "customer_needs": [], "buying_considerations": [],
            "seller_claims": ["Klarvo offers search."],
            "commercial_status": "A Klarvo trial was offered.",
            "agreed_actions": [{"task": "Open Klarvo", "owner": "customer participant",
                                "due_date": None, "evidence": "I'll open Klarvo"}],
        }
        corrected = _prefer_company_spelling(payload)
        self.assertIn("Clarvo", format_chat_message("Demo", "source", "brief", corrected))
        self.assertNotIn("Klarvo", format_chat_message("Demo", "source", "brief", corrected))
        self.assertEqual(corrected["agreed_actions"][0]["evidence"], "I'll open Klarvo")

    def test_evidence_inventory_discards_unquoted_claims_and_bounds_long_meetings(self):
        client = Mock()
        client.models.generate_content.return_value = Mock(
            parsed=EvidenceInventory(points=[
                EvidencePoint(point="Confirmed Slack", quote="We use Slack.",
                              source="customer", kind="current_tool"),
                EvidencePoint(point="Unconfirmed ATS", quote="We use Lever.",
                              source="customer", kind="current_tool"),
            ]),
            candidates=[],
        )
        points = _extract_evidence_inventory(client, "model", "We use Slack.")
        self.assertEqual([point["point"] for point in points], ["Confirmed Slack"])
        self.assertEqual(_extract_evidence_inventory(client, "model", "x" * (MAX_INVENTORY_CHARS + 1)), [])
        client.models.generate_content.assert_called_once()

    def test_published_customer_details_follow_attributed_quotes(self):
        draft = MeetingBrief.model_validate({
            **BRIEF,
            "meeting_type": "unknown", "classification_confidence": "low",
            "customer_needs": ["They use Lever as their ATS."],
            "buying_considerations": ["They have 120 users."],
        })
        inventory = [
            {"point": "Asked how their ATS would work with the platform.",
             "quote": "How does our ATS work with this?", "source": "customer", "kind": "question"},
            {"point": "Lever is one supported ATS integration.",
             "quote": "Lever is supported.", "source": "seller", "kind": "claim"},
        ]
        grounded = _ground_customer_sections(draft, inventory)
        self.assertEqual(grounded.customer_needs, [])
        self.assertEqual(grounded.buying_considerations,
                         ["Asked how their ATS would work with the platform."])

        internal = MeetingBrief.model_validate({
            **BRIEF,
            "customer_needs": ["A customer requirement inferred from an internal chat."],
            "seller_claims": ["A sales claim inferred from an internal chat."],
            "commercial_status": "A sale inferred from an internal chat.",
        })
        internal = _ground_customer_sections(internal, inventory)
        self.assertEqual(internal.customer_needs, [])
        self.assertEqual(internal.seller_claims, [])
        self.assertIsNone(internal.commercial_status)

    def test_webhook_posts_without_unsupported_request_id(self):
        response = Mock(status_code=200)
        response.json.return_value = {"name": "spaces/MEETINGS/messages/one"}
        with patch("meeting_analysis.requests.post", return_value=response) as post:
            url = "https://chat.googleapis.com/v1/spaces/MEETINGS/messages?key=k&token=t"
            result = post_chat_message(url, "MEETINGS", "source-key", "brief text")
        self.assertEqual(result, "spaces/MEETINGS/messages/one")
        self.assertNotIn("requestId=", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"], {"text": "brief text"})

    def test_worker_resumes_saved_brief_without_reanalysis(self):
        state = Mock()
        state.claim_for_work.return_value = {
            "output_doc_id": "transcript",
            "analysis_payload": BRIEF,
            "analysis_brief_doc_id": "brief",
            "analysis_chat_text": "saved message",
        }
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "transcript", "name": "Team planning - English Transcript",
            "parents": ["shared-folder"],
        }
        with patch("analysis_job.generate_report") as analyze, \
             patch("analysis_job.create_or_find_brief") as create, \
             patch("analysis_job.post_chat_message", return_value="message") as post:
            result = work_once("source-key", state, drive, Mock(), "key", "webhook", "MEETINGS")
        self.assertEqual(result, "brief")
        analyze.assert_not_called()
        create.assert_not_called()
        post.assert_called_once_with("webhook", "MEETINGS", "source-key", "saved message")
        state.mark_sending.assert_called_once_with("source-key")
        state.mark_posted.assert_called_once_with("source-key", "message")

    def test_worker_uses_full_report_for_new_analysis(self):
        state = Mock()
        state.claim_for_work.return_value = {"output_doc_id": "transcript"}
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "transcript", "name": "Planning - English Transcript",
            "parents": ["shared-folder"],
        }
        drive.files.return_value.export.return_value.execute.return_value = (
            b"Planning - English Transcript\nMethod: Meet transcript\n\n"
            b"Juha Lehto: We should review the plan.\n"
            b"Robert Holmberg: Yes, let's do that."
        )
        report = {"headline": "Planning update", "meeting_type": "unknown",
                  "classification_confidence": "low", "topic_tags": ["operations"],
                  "meeting_evidence": {}, "verified_companies": [],
                  "overview": ["The plan was discussed."], "discussion_sections": [],
                  "questions_and_answers": [], "next_steps": [], "open_questions": []}
        with patch("analysis_job.generate_report", return_value=report) as generate, \
             patch("analysis_job.create_or_find_brief", return_value="brief") as create, \
             patch("analysis_job.post_chat_message", return_value="message") as post:
            result = work_once("source-key", state, drive, Mock(), "key", "webhook",
                               "MEETINGS", company_references=[{"name": "Example"}])
        self.assertEqual(result, "brief")
        self.assertEqual(generate.call_args.args[0], "key")
        self.assertEqual(generate.call_args.args[-1], [{"name": "Example"}])
        self.assertEqual(create.call_args.kwargs["schema_version"], "2")
        self.assertIn("Full transcript", post.call_args.args[-1])
        self.assertIn("#operations", post.call_args.args[-1])

    def test_uncertain_chat_delivery_is_not_retried(self):
        from meeting_analysis import ChatDeliveryUncertain
        state = Mock()
        state.claim_for_work.return_value = {
            "output_doc_id": "transcript", "analysis_payload": BRIEF,
            "analysis_brief_doc_id": "brief", "analysis_chat_text": "saved message",
        }
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "transcript", "name": "Team planning - English Transcript",
            "parents": ["shared-folder"],
        }
        with patch("analysis_job.post_chat_message", side_effect=ChatDeliveryUncertain("timeout")):
            with self.assertRaises(ChatDeliveryUncertain):
                work_once("source-key", state, drive, Mock(), "key", "webhook", "MEETINGS")
        state.mark_delivery_unknown.assert_called_once()
        state.mark_retry.assert_called_once_with("source-key", unittest.mock.ANY, "processing")

    def test_worker_skips_archived_doc_without_spoken_words(self):
        state = Mock()
        state.claim_for_work.return_value = {"output_doc_id": "transcript"}
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "id": "transcript", "name": "Demo - English Transcript",
            "parents": ["shared-folder"],
        }
        drive.files.return_value.export.return_value.execute.return_value = (
            b"Demo - English Transcript\nMethod: Meet transcript\n\n"
            b"Demo - Transcript\nTranscription ended after 00:00:09\n"
        )
        with patch("analysis_job.generate_report") as analyze, \
             patch("analysis_job.post_chat_message") as post:
            result = work_once("source-key", state, drive, Mock(), "key", "webhook", "MEETINGS")
        self.assertIsNone(result)
        state.mark_invalid_source.assert_called_once()
        analyze.assert_not_called()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
