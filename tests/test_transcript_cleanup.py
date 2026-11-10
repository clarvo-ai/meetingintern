"""Exercise cleanup boundaries, audio alignment and fail-closed validation."""

import json
import re
import unittest
from unittest.mock import Mock

from transcript_cleanup import CleanupAudit, clean_transcript, parse_spelling_hints


def valid_reply(prompt, schema=None):
    if schema and issubclass(schema, CleanupAudit):
        targets = json.loads(prompt.rsplit("Audit segments: ", 1)[1])
        return json.dumps({"segments": [{"id": item["id"], "faithful": True} for item in targets]})
    target_text = prompt.rsplit("Segments: ", 1)[1]
    target_text = target_text.split("\nReview feedback", 1)[0].split("\nYour previous", 1)[0]
    targets = json.loads(target_text)
    return json.dumps({"segments": [{"id": item["id"],
                                      "text": item["text"].replace("Inner Sense", "Enersense")}
                                     for item in targets]})


class CleanupTests(unittest.TestCase):
    def test_verified_spelling_hints_are_configurable_and_validated(self):
        self.assertEqual(parse_spelling_hints('["Codex", " Codex "]'), ["Codex"])
        with self.assertRaises(ValueError):
            parse_spelling_hints('["Codex", ""]')

    def test_cleans_speech_with_audio_and_preserves_markers_and_speakers(self):
        original = ("Team recap - Transcript\n00:00:00\n"
                    "Robert Holmberg: Inner Sense starts a trial for 3 seats.\n"
                    "00:15:02\nMiro Keimiöniemi: We should check the connection.\n"
                    "Transcription ended after 00:16:00\n"
                    "This editable transcript was computer generated and might contain errors.")
        model = Mock()
        model._with_uploaded_audio.side_effect = lambda _path, prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        result = clean_transcript(original, model, {"company_directory": ["Enersense"]},
                                  audio_chunks=["audio-0", "audio-1"])
        self.assertIn("00:00:00\nRobert Holmberg: Enersense starts a trial for 3 seats.", result)
        self.assertIn("00:15:02\nMiro Keimiöniemi: We should check the connection.", result)
        self.assertIn("Transcription ended after 00:16:00", result)
        self.assertNotIn("computer generated", result)
        self.assertEqual([call.args[0] for call in model._with_uploaded_audio.call_args_list],
                         ["audio-0", "audio-0", "audio-1", "audio-1"])
        self.assertIn("Adjacent speech is context only", model._with_uploaded_audio.call_args_list[0].args[1])
        model._ask_text.assert_not_called()

    def test_text_only_and_ambiguous_names_do_not_gain_speaker_labels(self):
        model = Mock()
        model._ask_text.side_effect = lambda prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        result = clean_transcript("Speaker 1: I cannot set it up.\nSpeaker 2: Let's check.", model)
        self.assertEqual(result, "Speaker 1: I cannot set it up.\nSpeaker 2: Let's check.")
        model._with_uploaded_audio.assert_not_called()

    def test_speaker_spelling_uses_only_an_unambiguous_full_name(self):
        model = Mock()
        model._ask_text.side_effect = lambda prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        context = {"team_roster": ["Miro Keimiöniemi"]}
        result = clean_transcript("Miro Keimioniemi: Hello.\nMiro: Hi.\nSpeaker 1: Yes.", model, context)
        self.assertEqual(result, "Miro Keimiöniemi: Hello.\nMiro: Hi.\nSpeaker 1: Yes.")
        ambiguous = {"team_roster": ["Miro Keimiöniemi", "Miro Keimioniemi"]}
        result = clean_transcript("Miro Keimioniemi: Hello.", model, ambiguous)
        self.assertEqual(result, "Miro Keimioniemi: Hello.")

    def test_attendees_and_times_do_not_become_speaker_turns(self):
        model = Mock()
        model._ask_text.side_effect = lambda prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        original = "Attendees\nMiro Keimiöniemi\nTranscript\n12:30 is the start.\nMiro: Yes."
        self.assertEqual(clean_transcript(original, model), original)
        prompt = model._ask_text.call_args_list[0].args[0]
        self.assertIn('"text": "12:30 is the start."', prompt)
        self.assertNotIn('"text": "Miro Keimi', prompt)

    def test_retries_invalid_or_missing_segments_then_fails(self):
        model = Mock()
        model._ask_text.return_value = '{"segments": []}'
        with self.assertRaisesRegex(RuntimeError, "failed validation"):
            clean_transcript("Miro: We should review the deployment.", model)
        self.assertEqual(model._ask_text.call_count, 3)

    def test_one_unrecoverable_recorded_word_is_marked_unclear(self):
        model = Mock()
        model._with_uploaded_audio.return_value = '{"segments": []}'
        result = clean_transcript("00:01:00\nJuha: Heat.", model, audio_chunks=["audio"])
        self.assertEqual(result, "00:01:00\nJuha: [unclear]")

    def test_unrecoverable_numeric_or_long_turn_still_fails(self):
        for speech in ("17.", "I agreed to the price."):
            model = Mock()
            model._with_uploaded_audio.return_value = '{"segments": []}'
            with self.assertRaisesRegex(RuntimeError, "failed validation"):
                clean_transcript("00:01:00\nJuha: " + speech, model, audio_chunks=["audio"])

    def test_uncertain_audio_retries_with_independently_audited_text(self):
        model = Mock()
        model._with_uploaded_audio.return_value = '{"segments": []}'
        model._ask_text.side_effect = lambda prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        result = clean_transcript("00:01:00\nMark: We should review the issue.",
                                  model, audio_chunks=["audio"])
        self.assertEqual(result, "00:01:00\nMark: We should review the issue.")
        self.assertEqual(model._with_uploaded_audio.call_count, 3)
        self.assertEqual(model._ask_text.call_count, 2)
        self.assertTrue(issubclass(model._ask_text.call_args_list[1].kwargs["response_schema"], CleanupAudit))

    def test_rejects_changed_numbers_and_inserted_speaker(self):
        for response in ('{"segments":[{"id":"0","text":"We need 30 seats."}]}',
                         '{"segments":[{"id":"0","text":"Alex: We need 3 seats."}]}'):
            model = Mock()
            model._ask_text.return_value = response
            with self.assertRaisesRegex(RuntimeError, "failed validation"):
                clean_transcript("Miro: We need 3 seats.", model)

    def test_failed_faithfulness_audit_blocks_publication(self):
        def reply(prompt, _limit, **kw):
            if issubclass(kw["response_schema"], CleanupAudit):
                return '{"segments":[{"id":"0","faithful":false}]}'
            return valid_reply(prompt)
        model = Mock()
        model._ask_text.side_effect = reply
        with self.assertRaisesRegex(RuntimeError, "failed validation"):
            clean_transcript("Miro: We need to verify the CRM records.", model)

    def test_rejects_words_moved_across_speaker_turns(self):
        model = Mock()
        model._ask_text.return_value = json.dumps({"segments": [
            {"id": "0", "text": "Miro asked."},
            {"id": "1", "text": "Yes."},
            {"id": "2", "text": "Have you managed to set up Codex?"}]})
        original = ("Miro: Have you managed\nJuha: Yes.\n"
                    "Miro: to set up codecs?")
        with self.assertRaisesRegex(RuntimeError, "failed validation"):
            clean_transcript(original, model)

    def test_removing_a_filler_is_not_mistaken_for_copying_another_turn(self):
        def reply(prompt, _limit, **kw):
            if issubclass(kw['response_schema'], CleanupAudit):
                return valid_reply(prompt, kw['response_schema'])
            items = json.loads(prompt.rsplit('Segments: ', 1)[1])
            return json.dumps({'segments': [{'id': item['id'],
                'text': item['text'].replace(' uh ', ' ')} for item in items]})
        model = Mock()
        model._ask_text.side_effect = reply
        original = 'Speaker 1: We can uh review it tomorrow.\nSpeaker 2: We can review it tomorrow.'
        self.assertEqual(clean_transcript(original, model),
                         'Speaker 1: We can review it tomorrow.\nSpeaker 2: We can review it tomorrow.')

    def test_removing_negation_is_still_flagged_as_cross_turn_copying(self):
        from transcript_cleanup import _cross_segment_copies
        first = {'id': '1', 'text': 'We can not review it tomorrow.'}
        second = {'id': '2', 'text': 'We can review it tomorrow.'}
        self.assertEqual(_cross_segment_copies([first], [second['text']], [first, second]), {'1'})

    def test_interrupted_sentences_keep_each_speaker_turn(self):
        model = Mock()
        model._ask_text.side_effect = lambda prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        original = ("Juha: The summaries can cover internal meetings and so\n"
                    "Mark: Thank you.\n"
                    "Juha: on. We can add more later.")
        self.assertEqual(clean_transcript(original, model), original)
        self.assertIn("continue a sentence interrupted", model._ask_text.call_args_list[1].args[0])

    def test_splits_batch_when_auditor_rejects_one_of_many_turns(self):
        def reply(prompt, _limit, **kw):
            if issubclass(kw["response_schema"], CleanupAudit):
                targets = json.loads(prompt.rsplit("Audit segments: ", 1)[1])
                return json.dumps({"segments": [{"id": item["id"], "faithful": len(targets) == 1}
                                                for item in targets]})
            targets = json.loads(prompt.split("Segments: ", 1)[1].split("\nYour previous", 1)[0])
            return json.dumps({"segments": [{"id": item["id"], "text": item["text"]}
                                            for item in targets]})
        model = Mock()
        model._ask_text.side_effect = reply
        original = "Miro: I will check the connection.\nJuha: I will review the data."
        self.assertEqual(clean_transcript(original, model), original)
        self.assertGreater(model._ask_text.call_count, 4)

    def test_only_rejected_turns_are_regenerated(self):
        def reply(prompt, _limit, **kw):
            if issubclass(kw["response_schema"], CleanupAudit):
                targets = json.loads(prompt.rsplit("Audit segments: ", 1)[1])
                return json.dumps({"segments": [{"id": item["id"],
                                                  "faithful": len(targets) == 1 or item["id"] != "1"}
                                                 for item in targets]})
            return valid_reply(prompt)
        model = Mock()
        model._ask_text.side_effect = reply
        original = "Miro: I will check the connection.\nJuha: I will review the data."
        self.assertEqual(clean_transcript(original, model), original)
        self.assertEqual(model._ask_text.call_count, 4)

    def test_missing_or_unsafe_turns_are_retried_without_repeating_valid_ones(self):
        def reply(prompt, _limit, **kw):
            if issubclass(kw["response_schema"], CleanupAudit):
                return valid_reply(prompt, CleanupAudit)
            target_text = prompt.rsplit("Segments: ", 1)[1]
            targets = json.loads(target_text.split("\nReview feedback", 1)[0].split("\nYour previous", 1)[0])
            if len(targets) > 1:
                targets = targets[:1]
            return json.dumps({"segments": [{"id": item["id"], "text": item["text"]}
                                            for item in targets]})
        model = Mock()
        model._ask_text.side_effect = reply
        original = "Miro: I will check the connection.\nJuha: I will review 3 records."
        self.assertEqual(clean_transcript(original, model), original)
        self.assertEqual(model._ask_text.call_count, 4)

    def test_truncated_model_output_splits_large_batch(self):
        def reply(prompt, _limit, **kw):
            schema = kw["response_schema"]
            if not issubclass(schema, CleanupAudit):
                target_text = prompt.rsplit("Segments: ", 1)[1].split("\nYour previous", 1)[0]
                if len(json.loads(target_text)) > 1:
                    raise RuntimeError("Gemini text response exceeded the output limit")
            return valid_reply(prompt, schema)
        model = Mock()
        model._ask_text.side_effect = reply
        original = "Miro: Check the data.\nJuha: Review the result."
        self.assertEqual(clean_transcript(original, model), original)

    def test_eight_hour_transcript_retains_all_sections_in_order(self):
        original = "\n".join(
            f"[Chunk starting {minute // 60:02d}:{minute % 60:02d}:00]\n"
            f"Speaker 1: We discussed item {index}."
            for index, minute in enumerate(range(0, 8 * 60, 15)))
        model = Mock()
        model._with_uploaded_audio.side_effect = lambda _path, prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        result = clean_transcript(original, model, audio_chunks=list(range(32)))
        self.assertEqual(re.findall(r"\[Chunk starting [^\]]+\]", result),
                         re.findall(r"\[Chunk starting [^\]]+\]", original))
        self.assertEqual([int(value) for value in re.findall(r"item (\d+)", result)], list(range(32)))
        self.assertEqual(model._with_uploaded_audio.call_count, 64)

    def test_many_short_turns_fit_provider_schema_and_remain_in_order(self):
        from google.genai.errors import ClientError
        def reply(_path, prompt, _limit, **kw):
            audit = issubclass(kw['response_schema'], CleanupAudit)
            items = json.loads(prompt.rsplit('Audit segments: ' if audit else 'Segments: ', 1)[1])
            if len(items) > 50:
                raise ClientError(400, {'error': {'message': 'invalid argument', 'status': 'INVALID_ARGUMENT'}})
            return valid_reply(prompt, kw['response_schema'])
        model = Mock()
        model._with_uploaded_audio.side_effect = reply
        original = '\n'.join(f'Speaker 1: We discussed item {i}.' for i in range(97))
        self.assertEqual(clean_transcript(original, model, audio_chunks=['audio']), original)

    def test_verified_company_mentions_are_available_across_audio_chunks(self):
        model = Mock()
        model._with_uploaded_audio.side_effect = lambda _path, prompt, _limit, **kw: valid_reply(prompt, kw.get("response_schema"))
        source = ("[Chunk starting 00:00:00]\nRobert: Hawk Hunt asked for a demo.\n"
                  "[Chunk starting 00:15:00]\nJuha: What about Hulk Hunt?")
        context = {"company_directory": [{"name": "Hoxhunt", "aliases": ["Hawk Hunt"]}]}
        clean_transcript(source, model, context, audio_chunks=["first", "second"])
        later_prompt = model._with_uploaded_audio.call_args_list[2].args[1]
        self.assertIn('"companies_mentioned_in_full_transcript"', later_prompt)
        self.assertIn('"name": "Hoxhunt"', later_prompt)
        self.assertIn('"source_forms": ["Hawk Hunt"]', later_prompt)


if __name__ == "__main__":
    unittest.main()
