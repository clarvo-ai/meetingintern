"""An existing transcript is only changed after cleanup and revision checks."""

import unittest
import os
import tempfile
from unittest.mock import Mock, patch

from audio_archive import DOC_MIME
from transcript_backfill import _document_text, backfill_one


def plain_document():
    text = ("Meeting - English Transcript\n\n"
            "Source recording: not available after three hours\n"
            "Source notes: https://docs.google.com/document/d/source/edit\n"
            "Method: Google Meet transcript (English)\n\n"
            "00:00:00\nMiro: Inner Sense starts a trial.\n")
    return {"revisionId": "rev-1", "tabs": [{"tabProperties": {"tabId": "t.0"},
            "documentTab": {"body": {"content": [
                {"endIndex": 1, "sectionBreak": {}},
                {"endIndex": len(text) + 1, "paragraph": {"elements": [
                    {"textRun": {"content": text, "textStyle": {}}}]}}]}}}]}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.drive, self.docs, self.model = Mock(), Mock(), Mock()
        self.drive.files.return_value.get.return_value.execute.return_value = {
            "id": "doc", "name": "Meeting", "mimeType": DOC_MIME,
            "parents": ["destination"], "properties": {"source_key": "source"}}
        self.docs.documents.return_value.get.return_value.execute.return_value = plain_document()
        self.state = {"status": "done", "output_doc_id": "doc"}

    def run_backfill(self, apply_from=None, preview_dir=None):
        return backfill_one("source", self.state, self.drive, self.docs, self.model,
                            "destination", None, [], None, {}, [],
                            apply_from=apply_from, preview_dir=preview_dir)

    def test_preview_does_not_change_company_drive(self):
        with patch("transcript_backfill.clean_transcript", return_value="Miro: Enersense starts a trial."):
            self.assertIn("preview ready", self.run_backfill())
        self.docs.documents.return_value.batchUpdate.assert_not_called()
        self.drive.files.return_value.update.assert_not_called()

    def test_preview_file_contains_only_cleaned_text_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("transcript_backfill.clean_transcript", return_value="Miro: Enersense starts a trial."):
            backfill_one("source", self.state, self.drive, self.docs, self.model,
                         "destination", None, [], None, {}, [], preview_dir=directory)
            path = os.path.join(directory, "source.txt")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, encoding="utf-8") as preview:
                self.assertIn("Miro: Enersense starts a trial.", preview.read())
        self.docs.documents.return_value.batchUpdate.assert_not_called()

    def test_apply_preserves_link_header_and_requires_original_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("transcript_backfill.clean_transcript", return_value="00:00:00\nMiro: Enersense starts a trial.") as clean:
                self.run_backfill(preview_dir=directory)
                self.assertEqual(self.run_backfill(apply_from=directory), "cleaned")
                clean.assert_called_once()
        request = self.docs.documents.return_value.batchUpdate.call_args.kwargs
        self.assertEqual(request["documentId"], "doc")
        self.assertEqual(request["body"]["writeControl"]["requiredRevisionId"], "rev-1")
        inserted = request["body"]["requests"][1]["insertText"]
        self.assertEqual(inserted["location"]["tabId"], "t.0")
        self.assertIn("Source notes: https://docs.google.com/document/d/source/edit", inserted["text"])
        self.assertIn("Method: Google Meet transcript (English); AI-cleaned", inserted["text"])
        self.assertIn("Miro: Enersense starts a trial.", inserted["text"])
        self.assertEqual(self.drive.files.return_value.update.call_args.kwargs["body"]["properties"]
                         ["transcript_cleanup_version"], "1")

    def test_failed_cleanup_never_replaces_existing_doc(self):
        with patch("transcript_backfill.clean_transcript", side_effect=RuntimeError("Incomplete")):
            with self.assertRaisesRegex(RuntimeError, "Incomplete"):
                self.run_backfill()
        self.docs.documents.return_value.batchUpdate.assert_not_called()

    def test_revision_conflict_does_not_mark_document_clean(self):
        self.docs.documents.return_value.batchUpdate.return_value.execute.side_effect = RuntimeError("revision conflict")
        with tempfile.TemporaryDirectory() as directory, \
             patch("transcript_backfill.clean_transcript", return_value="Cleaned speech"):
            self.run_backfill(preview_dir=directory)
            with self.assertRaisesRegex(RuntimeError, "revision conflict"):
                self.run_backfill(apply_from=directory)
        self.drive.files.return_value.update.assert_not_called()

    def test_stale_or_modified_preview_cannot_be_applied(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("transcript_backfill.clean_transcript", return_value="Cleaned speech"):
            self.run_backfill(preview_dir=directory)
            self.docs.documents.return_value.get.return_value.execute.return_value["revisionId"] = "new-revision"
            with self.assertRaisesRegex(ValueError, "stale"):
                self.run_backfill(apply_from=directory)
        self.docs.documents.return_value.batchUpdate.assert_not_called()

    def test_modified_preview_cannot_be_applied(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("transcript_backfill.clean_transcript", return_value="Cleaned speech"):
            self.run_backfill(preview_dir=directory)
            with open(os.path.join(directory, "source.txt"), "a", encoding="utf-8") as preview:
                preview.write("modified")
            with self.assertRaisesRegex(ValueError, "modified"):
                self.run_backfill(apply_from=directory)
        self.docs.documents.return_value.batchUpdate.assert_not_called()

    def test_refuses_other_documents_and_complex_tabs(self):
        self.drive.files.return_value.get.return_value.execute.return_value["parents"] = ["elsewhere"]
        with self.assertRaisesRegex(ValueError, "not an archive"):
            self.run_backfill(apply_from="/unused")
        doc = plain_document()
        doc["tabs"].append(doc["tabs"][0])
        with self.assertRaisesRegex(ValueError, "multiple tabs"):
            _document_text(doc)


if __name__ == "__main__":
    unittest.main()
