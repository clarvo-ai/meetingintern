"""Focused checks for the brief and Chat delivery contract."""

import unittest
from unittest.mock import Mock, patch

from meeting_analysis import format_brief, format_chat_message, post_chat_message
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
        with patch("analysis_job.analyze_transcript") as analyze, \
             patch("analysis_job.create_or_find_brief") as create, \
             patch("analysis_job.post_chat_message", return_value="message") as post:
            result = work_once("source-key", state, drive, Mock(), "key", "webhook", "MEETINGS")
        self.assertEqual(result, "brief")
        analyze.assert_not_called()
        create.assert_not_called()
        post.assert_called_once_with("webhook", "MEETINGS", "source-key", "saved message")
        state.mark_sending.assert_called_once_with("source-key")
        state.mark_posted.assert_called_once_with("source-key", "message")

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
        with patch("analysis_job.analyze_transcript") as analyze, \
             patch("analysis_job.post_chat_message") as post:
            result = work_once("source-key", state, drive, Mock(), "key", "webhook", "MEETINGS")
        self.assertIsNone(result)
        state.mark_invalid_source.assert_called_once()
        analyze.assert_not_called()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
