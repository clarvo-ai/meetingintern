"""Exercise the meeting-ready outbox without external services."""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis_job import work_once
from analysis_state import AnalysisState
from archive_state import ArchiveState
from meeting_ready import TODO_WEBHOOK_URL, WEBHOOK_URL, load_config, make_event, validate_event
from meeting_ready_worker import MeetingReadyOutbox, _id_token, deliver, run_once


KEY = "a" * 64
CUTOFF = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
ACCOUNT = "drive-automation@drive-atuomation-project.iam.gserviceaccount.com"
ENV = {"MEETING_READY_WEBHOOK_URL": WEBHOOK_URL,
       "MEETING_READY_CUTOVER": CUTOFF.isoformat(),
       "MEETING_READY_SERVICE_ACCOUNT_EMAIL": ACCOUNT}
CONFIG = load_config(ENV)


class Snapshot:
    def __init__(self, key, data):
        self.id, self._data, self.exists = key, dict(data), bool(data)

    def to_dict(self):
        return dict(self._data)


class Reference:
    def __init__(self, key):
        self.key, self.data = key, {}

    def get(self, transaction=None):
        return Snapshot(self.key, self.data)

    def update(self, values):
        self.data.update(values)


class Transaction:
    def update(self, ref, values):
        ref.data.update(values)

    def set(self, ref, values, merge=False):
        ref.data.update(values)


class Collection:
    def __init__(self):
        self.refs = {}
        self.query = None

    def document(self, key):
        return self.refs.setdefault(key, Reference(key))

    def where(self, **kwargs):
        self.query = kwargs["filter"]
        return self

    def stream(self):
        expected = self.query.value
        return [ref.get() for ref in self.refs.values()
                if (ref.data.get(self.query.field_path) in expected
                    if self.query.op_string == "in" else
                    ref.data.get(self.query.field_path) == expected)]


class Client:
    def transaction(self):
        return Transaction()


def archive(start=CUTOFF + timedelta(minutes=2), first_seen=CUTOFF + timedelta(minutes=3),
            verified=CUTOFF + timedelta(minutes=2)):
    return {"status": "done", "output_doc_id": "transcript_123",
            "meeting_started_at": start, "archive_first_seen_at": first_seen,
            "meeting_ready_verified_start_at": verified}


def token_for(audience=WEBHOOK_URL, email=ACCOUNT):
    raw = json.dumps({"aud": audience, "email": email}).encode()
    return "header." + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ".signature"


class ContractTests(unittest.TestCase):
    def test_configuration_is_disabled_by_default_and_fails_closed(self):
        self.assertIsNone(load_config({}))
        for patch_values in ({"MEETING_READY_CUTOVER": ""},
                             {"MEETING_READY_WEBHOOK_URL": WEBHOOK_URL + "/"},
                             {"MEETING_READY_SERVICE_ACCOUNT_EMAIL": "not-an-account"},
                             {"MEETING_READY_CUTOVER": "2026-11-05T12:00:00"},
                             {"MEETING_READY_CUTOVER": "garbage"},
                             {"MEETING_TODO_WEBHOOK_URL": "https://other.example/"}):
            with self.subTest(patch_values=patch_values), self.assertRaises(ValueError):
                load_config({**ENV, **patch_values})

    def test_event_contains_only_the_exact_contract(self):
        event = make_event(KEY, archive(), "brief_123", CONFIG)
        self.assertEqual(event, {"schema_version": 1, "event_id": KEY,
                                 "source": "meetingintern", "meeting_id": KEY,
                                 "transcript_file_id": "transcript_123", "brief_file_id": "brief_123"})
        for bad in ({**event, "text": "private"}, {**event, "meeting_id": "b" * 64},
                    {**event, "event_id": KEY.upper()}, {**event, "brief_file_id": "x@example.com"},
                    {**event, "brief_file_id": event["transcript_file_id"]},
                    {**event, "schema_version": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_event(bad)

    def test_cutover_excludes_old_and_preexisting_meetings(self):
        self.assertIsNone(make_event(KEY, archive(start=CUTOFF - timedelta(seconds=1)), "brief", CONFIG))
        self.assertIsNone(make_event(KEY, archive(first_seen=CUTOFF - timedelta(seconds=1)), "brief", CONFIG))
        self.assertIsNone(make_event(KEY, archive(first_seen=None), "brief", CONFIG))
        self.assertIsNone(make_event(KEY, archive(verified=None), "brief", CONFIG))
        self.assertIsNone(make_event(KEY, archive(verified=CUTOFF - timedelta(seconds=1)), "brief", CONFIG))
        self.assertIsNotNone(make_event(KEY, archive(start=datetime(2026, 11, 5)), "brief", CONFIG))
        self.assertIsNone(make_event(KEY, {**archive(), "status": "processing"}, "brief", CONFIG))
        self.assertIsNotNone(make_event(KEY, archive(start=CUTOFF, first_seen=CUTOFF), "brief", CONFIG))

    def test_token_uses_exact_audience_and_expected_service_account(self):
        with patch("meeting_ready_worker.id_token.fetch_id_token", return_value=token_for()) as fetch:
            self.assertEqual(_id_token(CONFIG), token_for())
            self.assertEqual(fetch.call_args.args[1], WEBHOOK_URL)
        for bad in (token_for(audience=WEBHOOK_URL + "/"), token_for(email="other@example.com"), "bad"):
            with patch("meeting_ready_worker.id_token.fetch_id_token", return_value=bad):
                with self.assertRaises(ValueError):
                    _id_token(CONFIG)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.collection = Collection()
        self.client = Client()
        self.analysis = AnalysisState.__new__(AnalysisState)
        self.analysis.client, self.analysis.collection = self.client, self.collection
        self.outbox = MeetingReadyOutbox.__new__(MeetingReadyOutbox)
        self.outbox.client, self.outbox.collection = self.client, self.collection
        self.ref = self.collection.document(KEY)
        self.ref.data.update(archive())
        self.payload = {"meeting_type": "unknown", "topic_tags": []}
        self.patches = [patch("analysis_state.firestore.transactional", lambda function: function),
                        patch("meeting_ready_worker.firestore.transactional", lambda function: function)]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, config=CONFIG):
        self.analysis.save_brief(KEY, "brief_123", self.payload, "Chat text", ready_config=config)

    def test_brief_and_outbox_are_saved_together_without_chat_status(self):
        self.save()
        self.assertEqual(self.ref.data["analysis_brief_doc_id"], "brief_123")
        self.assertEqual(self.ref.data["meeting_ready_event"], make_event(KEY, archive(), "brief_123", CONFIG))
        self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
        self.assertNotIn("analysis_status", self.ref.data)

    def test_disabled_and_pre_cutover_paths_do_not_queue(self):
        self.save(None)
        self.assertNotIn("meeting_ready_event", self.ref.data)
        self.ref.data["archive_first_seen_at"] = CUTOFF - timedelta(seconds=1)
        self.save()
        self.assertNotIn("meeting_ready_event", self.ref.data)
        self.assertEqual(self.ref.data["analysis_chat_text"], "Chat text")

    def test_matched_calendar_provides_missing_start_and_old_start_vetoes(self):
        self.ref.data["meeting_ready_verified_start_at"] = None
        self.payload["meeting_evidence"] = {"calendar_match": "matched",
            "calendar_event": {"start": (CUTOFF + timedelta(minutes=1)).isoformat()}}
        self.save()
        self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
        self.assertEqual(self.ref.data["meeting_ready_verified_start_at"], CUTOFF + timedelta(minutes=1))

        self.ref.data = archive()
        self.payload["meeting_evidence"]["calendar_event"]["start"] = (CUTOFF - timedelta(minutes=1)).isoformat()
        self.save()
        self.assertNotIn("meeting_ready_event", self.ref.data)
        self.payload["meeting_evidence"]["calendar_match"] = "ambiguous"
        self.ref.data["meeting_ready_verified_start_at"] = None
        self.save()
        self.assertNotIn("meeting_ready_event", self.ref.data)

    def test_repeat_save_never_replaces_existing_event_or_delivery_state(self):
        self.save()
        original = dict(self.ref.data["meeting_ready_event"])
        self.ref.data["meeting_ready_status"] = "delivered"
        self.analysis.save_brief(KEY, "new_brief", self.payload, "new chat", ready_config=CONFIG)
        self.assertEqual(self.ref.data["meeting_ready_event"], original)
        self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")

    def test_transaction_retry_does_not_reset_a_delivered_event(self):
        def retry_once(function):
            def run(transaction):
                function(transaction)
                self.ref.data["meeting_ready_status"] = "delivered"
                function(transaction)
            return run
        with patch("analysis_state.firestore.transactional", retry_once):
            self.save()
        self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")

    def test_invalid_candidate_does_not_block_brief_save(self):
        self.ref.data["output_doc_id"] = "invalid@example.com"
        self.save()
        self.assertNotIn("meeting_ready_event", self.ref.data)
        self.assertEqual(self.ref.data["meeting_ready_status"], "review")
        self.assertEqual(self.ref.data["meeting_ready_reason"], "invalid_transcript_file_id")
        self.assertEqual(self.ref.data["analysis_brief_doc_id"], "brief_123")

    def test_unknown_time_and_bad_ids_are_queryable_without_chat_changes(self):
        self.ref.data.update({"meeting_ready_verified_start_at": None, "analysis_status": "posted",
                              "analysis_chat_message_name": "chat-message"})
        self.save()
        self.assertEqual((self.ref.data["meeting_ready_status"], self.ref.data["meeting_ready_reason"]),
                         ("review", "missing_verified_start"))
        self.assertEqual(self.ref.data["analysis_status"], "posted")
        self.assertEqual(self.ref.data["analysis_chat_message_name"], "chat-message")
        self.ref.data["meeting_ready_verified_start_at"] = CUTOFF + timedelta(minutes=1)
        self.ref.data["output_doc_id"] = "brief_123"
        self.save()
        self.assertEqual((self.ref.data["meeting_ready_status"], self.ref.data["meeting_ready_reason"]),
                         ("review", "identical_file_ids"))

    def test_old_meeting_is_excluded_even_when_other_evidence_is_missing(self):
        self.ref.data.update({"meeting_started_at": CUTOFF - timedelta(days=1),
                              "meeting_ready_verified_start_at": None})
        self.save()
        self.assertEqual((self.ref.data["meeting_ready_status"], self.ref.data["meeting_ready_reason"]),
                         ("excluded", "pre_cutover"))
        self.assertNotIn("meeting_ready_event", self.ref.data)

    def test_manual_reconciliation_preserves_chat_and_only_changes_outbox(self):
        self.ref.data.update({"meeting_ready_verified_start_at": None, "analysis_status": "posted",
                              "analysis_chat_message_name": "chat-message"})
        self.save()
        self.assertEqual(self.outbox.reconcile(KEY, CONFIG, manual=True), "review")
        self.assertEqual(self.outbox.reconcile(KEY, CONFIG, manual=True,
            verified_start=CUTOFF + timedelta(minutes=1), transcript_file_id="repaired_transcript"), "pending")
        self.assertEqual(self.ref.data["meeting_ready_event"]["transcript_file_id"], "repaired_transcript")
        self.assertEqual(self.ref.data["output_doc_id"], "transcript_123")
        self.assertEqual(self.ref.data["analysis_status"], "posted")
        self.assertEqual(self.ref.data["analysis_chat_message_name"], "chat-message")
        self.assertIsNone(self.outbox.reconcile(KEY, CONFIG, manual=True, brief_file_id="other"))

    def test_manual_review_cannot_override_pre_cutover_evidence(self):
        self.ref.data.update({"meeting_ready_status": "review", "analysis_brief_doc_id": "brief_123",
                              "analysis_payload": self.payload,
                              "meeting_ready_verified_start_at": CUTOFF - timedelta(minutes=1)})
        self.assertEqual(self.outbox.reconcile(KEY, CONFIG, manual=True,
                         verified_start=CUTOFF + timedelta(days=1)), "excluded")
        self.assertNotIn("meeting_ready_event", self.ref.data)

    def test_review_corrections_survive_until_all_problems_are_fixed(self):
        self.ref.data.update({"meeting_ready_verified_start_at": None, "output_doc_id": "bad@email"})
        self.save()
        self.assertEqual(self.outbox.reconcile(KEY, CONFIG, manual=True,
                         verified_start=CUTOFF + timedelta(minutes=1)), "review")
        self.assertEqual(self.ref.data["meeting_ready_reason"], "invalid_transcript_file_id")
        self.assertEqual(self.ref.data["meeting_ready_verified_start_at"], CUTOFF + timedelta(minutes=1))
        with self.assertRaises(ValueError):
            self.outbox.reconcile(KEY, CONFIG, manual=True, transcript_file_id="bad@email")
        self.assertEqual(self.outbox.reconcile(KEY, CONFIG, manual=True,
                         transcript_file_id="fixed_transcript"), "pending")
        self.assertEqual(self.ref.data["meeting_ready_event"]["transcript_file_id"], "fixed_transcript")

    def test_invalid_brief_and_source_key_have_safe_reason_codes(self):
        self.analysis.save_brief(KEY, "bad@example.com", self.payload, "Chat", ready_config=CONFIG)
        self.assertEqual(self.ref.data["meeting_ready_reason"], "invalid_brief_file_id")
        invalid = self.collection.document("invalid-key")
        invalid.data.update(archive())
        self.analysis.save_brief("invalid-key", "brief_123", self.payload, "Chat", ready_config=CONFIG)
        self.assertEqual(invalid.data["meeting_ready_reason"], "invalid_source_key")
        self.assertEqual(invalid.data["meeting_ready_status"], "review")

    def test_old_analysis_image_is_reconciled_after_new_checker(self):
        # Simulate a new checker, followed by an old image using the unconfigured
        # save path, then the independent worker picking up its saved brief.
        self.ref.data = {}
        archives = ArchiveState.__new__(ArchiveState)
        archives.client, archives.collection = self.client, self.collection
        meeting = SimpleNamespace(source_key=KEY, folder_id="folder", notes_id="doc",
                                  folder_name="Meeting", started_at=CUTOFF + timedelta(minutes=2),
                                  verified_started_at=CUTOFF + timedelta(minutes=2))
        with patch("archive_state.firestore.transactional", lambda function: function), \
             patch("archive_state._utc_now", return_value=CUTOFF + timedelta(minutes=3)):
            self.assertTrue(archives.reserve_for_dispatch(meeting, "root"))
        self.ref.data.update({"status": "done", "output_doc_id": "transcript_123",
                              "analysis_status": "posted", "analysis_chat_message_name": "existing-message"})
        self.analysis.save_brief(KEY, "brief_123", self.payload, "old chat")
        self.assertEqual(self.ref.data["meeting_ready_status"], "awaiting_brief")
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=lambda *_: 204), 1)
        self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")
        self.assertEqual(self.ref.data["analysis_status"], "posted")
        self.assertEqual(self.ref.data["analysis_chat_message_name"], "existing-message")

    def test_waiting_old_image_with_missing_date_is_flagged_for_review(self):
        self.ref.data.update({"meeting_ready_status": "awaiting_brief", "analysis_status": "posted",
                              "meeting_ready_verified_start_at": None,
                              "analysis_brief_doc_id": "brief_123", "analysis_payload": self.payload})
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=lambda *_: 200), 0)
        self.assertEqual((self.ref.data["meeting_ready_status"], self.ref.data["meeting_ready_reason"]),
                         ("review", "missing_verified_start"))
        self.assertEqual(self.ref.data["analysis_status"], "posted")

    def test_awaiting_brief_is_skipped_without_starving_ready_candidates(self):
        self.ref.data.update({"meeting_ready_status": "awaiting_brief", "analysis_status": "processing"})
        other = self.collection.document("b" * 64)
        other.data.update({**archive(), "meeting_ready_status": "awaiting_brief",
                           "analysis_brief_doc_id": "brief_123", "analysis_payload": self.payload})
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
            self.assertEqual(run_once(self.outbox, CONFIG, batch_size=1, deliver_event=lambda *_: 200), 1)
        self.assertEqual(self.ref.data["meeting_ready_status"], "awaiting_brief")
        self.assertEqual(other.data["meeting_ready_status"], "delivered")

    def test_claim_leases_and_fences_late_workers(self):
        self.save()
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)) as clock:
            first = self.outbox.claim(KEY, CONFIG)
            self.assertIsNotNone(first)
            self.assertIsNone(self.outbox.claim(KEY, CONFIG))
            clock.return_value += timedelta(minutes=11)
            second = self.outbox.claim(KEY, CONFIG)
            self.assertIsNotNone(second)
            self.assertFalse(self.outbox.finish(KEY, first[1], True, first[2], "http_200"))
            self.assertTrue(self.outbox.finish(KEY, second[1], True, second[2], "http_204"))
            self.assertIsNone(self.outbox.claim(KEY, CONFIG))

    def test_failure_retries_same_bytes_and_only_2xx_acknowledges(self):
        self.save()
        post = Mock(side_effect=[SimpleNamespace(status_code=503), SimpleNamespace(status_code=204)])
        sender = lambda event, config: deliver(event, config, token_factory=lambda _: "id-token", post=post)
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)) as clock:
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 1)
            self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 0)
            clock.return_value += timedelta(seconds=61)
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 1)
            self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 0)
        first, second = post.call_args_list
        self.assertEqual(first.args[0], WEBHOOK_URL)
        self.assertEqual(first.kwargs["data"], second.kwargs["data"])
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer id-token")
        self.assertFalse(first.kwargs["allow_redirects"])
        self.assertEqual(set(json.loads(first.kwargs["data"])),
                         {"schema_version", "event_id", "source", "meeting_id", "transcript_file_id", "brief_file_id"})

    def test_fanout_retries_until_both_receivers_acknowledge(self):
        config = load_config({**ENV, "MEETING_TODO_WEBHOOK_URL": TODO_WEBHOOK_URL})
        self.save()
        post = Mock(side_effect=[SimpleNamespace(status_code=202), SimpleNamespace(status_code=503),
                                 SimpleNamespace(status_code=200), SimpleNamespace(status_code=202)])
        audiences = []
        def get_token(target):
            audiences.append(target.url)
            return 'id-token'
        sender = lambda event, settings: deliver(event, settings, token_factory=get_token, post=post)
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)) as clock:
            self.assertEqual(run_once(self.outbox, config, deliver_event=sender), 1)
            self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
            clock.return_value += timedelta(seconds=61)
            self.assertEqual(run_once(self.outbox, config, deliver_event=sender), 1)
            self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")
        self.assertEqual(audiences, [WEBHOOK_URL, TODO_WEBHOOK_URL, WEBHOOK_URL, TODO_WEBHOOK_URL])
        self.assertEqual(len({call.kwargs['data'] for call in post.call_args_list}), 1)

    def test_unreachable_receiver_does_not_block_other_receiver_attempt(self):
        config = load_config({**ENV, "MEETING_TODO_WEBHOOK_URL": TODO_WEBHOOK_URL})
        event = make_event(KEY, archive(), "brief_123", config)
        post = Mock(side_effect=[RuntimeError('unavailable'), SimpleNamespace(status_code=202)])
        with self.assertRaises(RuntimeError):
            deliver(event, config, token_factory=lambda _: 'id-token', post=post)
        self.assertEqual([call.args[0] for call in post.call_args_list], [WEBHOOK_URL, TODO_WEBHOOK_URL])

    def test_lost_ack_retries_the_same_event_after_lease_expiry(self):
        self.save()
        post = Mock(return_value=SimpleNamespace(status_code=200))
        sender = lambda event, config: deliver(event, config, token_factory=lambda _: "token", post=post)
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)) as clock:
            with patch.object(self.outbox, "finish", side_effect=RuntimeError("state unavailable")):
                with self.assertRaises(RuntimeError):
                    run_once(self.outbox, CONFIG, deliver_event=sender)
            self.assertEqual(self.ref.data["meeting_ready_status"], "sending")
            clock.return_value += timedelta(minutes=11)
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 1)
        self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")
        self.assertEqual(post.call_args_list[0].kwargs["data"], post.call_args_list[1].kwargs["data"])

    def test_retry_keeps_original_event_when_brief_reference_changes(self):
        self.save()
        original = dict(self.ref.data["meeting_ready_event"])
        self.ref.data["analysis_brief_doc_id"] = "updated_brief"
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
            claim = self.outbox.claim(KEY, CONFIG)
        self.assertEqual(claim[0], original)
        self.assertEqual(claim[0]["brief_file_id"], "brief_123")

    def test_cutover_change_suppresses_old_queued_event(self):
        self.save()
        later = load_config({**ENV, "MEETING_READY_CUTOVER": (CUTOFF + timedelta(days=1)).isoformat()})
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(days=2)):
            self.assertIsNone(self.outbox.claim(KEY, later))
        self.assertEqual(self.ref.data["meeting_ready_status"], "suppressed")

    def test_redirect_and_network_failure_do_not_acknowledge(self):
        for failure in (302, 400, 500, RuntimeError("secret must not be logged")):
            with self.subTest(failure=type(failure).__name__):
                self.ref.data = archive()
                self.save()
                def sender(_event, _config):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
                    self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=sender), 1)
                self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
                self.assertNotIn("secret", self.ref.data.get("meeting_ready_last_result", ""))

    def test_disabled_worker_does_not_touch_outbox(self):
        store = Mock()
        self.assertEqual(run_once(store, None), 0)
        store.candidates.assert_not_called()

    def test_wrong_token_identity_never_sends_request(self):
        event = make_event(KEY, archive(), "brief", CONFIG)
        post = Mock()
        with patch("meeting_ready_worker.id_token.fetch_id_token", return_value=token_for(email="other@example.com")):
            with self.assertRaises(ValueError):
                deliver(event, CONFIG, post=post)
        post.assert_not_called()

    def test_chat_post_is_independent_of_delivery_worker(self):
        state, drive = Mock(), Mock()
        state.claim_for_work.return_value = {"output_doc_id": "transcript_123",
            "analysis_payload": self.payload, "analysis_brief_doc_id": "brief_123",
            "analysis_chat_text": "saved chat"}
        drive.files.return_value.get.return_value.execute.return_value = {
            "name": "Meeting - English Transcript", "parents": ["folder"]}
        with patch("analysis_job.post_chat_message", return_value="message") as post, \
             patch("meeting_ready_worker.deliver", side_effect=RuntimeError("receiver down")) as delivery:
            result = work_once(KEY, state, drive, Mock(), "key", "webhook", "space",
                               meeting_ready_config=CONFIG)
        self.assertEqual(result, "brief_123")
        post.assert_called_once()
        delivery.assert_not_called()
        state.save_brief.assert_called_once_with(KEY, "brief_123", self.payload, "saved chat", ready_config=CONFIG)

    def test_outbox_is_delivered_even_when_chat_delivery_is_uncertain(self):
        from meeting_analysis import ChatDeliveryUncertain
        self.ref.data.update({"analysis_status": "pending", "analysis_payload": self.payload,
                              "analysis_brief_doc_id": "brief_123", "analysis_chat_text": "saved chat"})
        drive = Mock()
        drive.files.return_value.get.return_value.execute.return_value = {
            "name": "Meeting - English Transcript", "parents": ["folder"]}
        with patch("analysis_job.post_chat_message", side_effect=ChatDeliveryUncertain("timeout")):
            with self.assertRaises(ChatDeliveryUncertain):
                work_once(KEY, self.analysis, drive, Mock(), "key", "webhook", "space",
                          meeting_ready_config=CONFIG)
        self.assertEqual(self.ref.data["analysis_status"], "delivery_unknown")
        self.assertEqual(self.ref.data["meeting_ready_status"], "pending")
        with patch("meeting_ready_worker._now", return_value=CUTOFF + timedelta(hours=1)):
            self.assertEqual(run_once(self.outbox, CONFIG, deliver_event=lambda *_: 200), 1)
        self.assertEqual(self.ref.data["meeting_ready_status"], "delivered")
        self.assertEqual(self.ref.data["analysis_status"], "delivery_unknown")


if __name__ == "__main__":
    unittest.main()
