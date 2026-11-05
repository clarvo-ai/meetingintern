"""Independent, scheduled delivery of durable meeting-ready events."""

import base64
import argparse
import functools
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import google.auth
import requests
from google.auth.transport.requests import Request
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import id_token

from meeting_ready import (_aware, eligible_archive, load_config, outbox_changes,
                           valid_file_id, validate_event)


LOGGER = logging.getLogger(__name__)
LEASE = timedelta(minutes=10)
MAX_BATCH = 25


def _now():
    return datetime.now(timezone.utc)


class MeetingReadyOutbox:
    """Lease each saved event independently from analysis and Chat status."""

    def __init__(self, project_id):
        self.client = firestore.Client(project=project_id)
        self.collection = self.client.collection("meeting_audio_archive")

    def candidates(self):
        snapshots = self.collection.where(filter=FieldFilter(
            "meeting_ready_status", "in", ["pending", "sending"])).stream()
        return [snapshot.id for snapshot in snapshots]

    def awaiting_brief(self):
        snapshots = self.collection.where(filter=FieldFilter(
            "meeting_ready_status", "==", "awaiting_brief")).stream()
        return [snapshot.id for snapshot in snapshots]

    def reconcile(self, key, config, *, manual=False, verified_start=None,
                  transcript_file_id=None, brief_file_id=None):
        """Queue a saved brief or record review; never alter Chat or an existing event."""
        ref = self.collection.document(key)
        if verified_start is not None and not _aware(verified_start):
            raise ValueError("Verified start must include a timezone")
        for value in (transcript_file_id, brief_file_id):
            if value is not None and not valid_file_id(value):
                raise ValueError("Invalid correction file ID")

        @firestore.transactional
        def reconcile_transaction(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            current = snapshot.to_dict()
            allowed = ("review",) if manual else ("awaiting_brief",)
            if (current.get("meeting_ready_status") not in allowed or
                    "meeting_ready_event" in current or current.get("status") != "done" or
                    "analysis_brief_doc_id" not in current):
                return None
            candidate = dict(current)
            if verified_start is not None:
                prior = candidate.get("meeting_ready_verified_start_at")
                candidate["meeting_ready_verified_start_at"] = (
                    min(prior, verified_start) if _aware(prior) else verified_start)
            selected_transcript = (transcript_file_id if transcript_file_id is not None else
                                   current.get("meeting_ready_override_transcript_file_id"))
            if selected_transcript is not None:
                candidate["output_doc_id"] = selected_transcript
            selected_brief = (brief_file_id if brief_file_id is not None else
                              current.get("meeting_ready_override_brief_file_id",
                                          current["analysis_brief_doc_id"]))
            changes = outbox_changes(key, candidate, selected_brief,
                                     current.get("analysis_payload") or {}, config, _now())
            if not changes:
                return None
            if manual and changes["meeting_ready_status"] in ("review", "pending"):
                if verified_start is not None:
                    changes["meeting_ready_verified_start_at"] = candidate["meeting_ready_verified_start_at"]
                if transcript_file_id is not None:
                    changes["meeting_ready_override_transcript_file_id"] = transcript_file_id
                if brief_file_id is not None:
                    changes["meeting_ready_override_brief_file_id"] = brief_file_id
            transaction.update(ref, changes)
            return changes["meeting_ready_status"]

        return reconcile_transaction(self.client.transaction())

    def claim(self, key, config):
        """Fence concurrent workers and reclaim a sender whose lease expired."""
        ref = self.collection.document(key)
        now = _now()
        claim_id = uuid.uuid4().hex

        @firestore.transactional
        def claim_transaction(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            status = data.get("meeting_ready_status")
            if status not in ("pending", "sending"):
                return None
            if status == "sending" and data.get("meeting_ready_lease_until", now) > now:
                return None
            if data.get("meeting_ready_next_attempt_at", now) > now:
                return None
            event = data.get("meeting_ready_event")
            try:
                validate_event(event)
                permitted = event["event_id"] == key and eligible_archive(data, config)
            except ValueError:
                transaction.update(ref, {"meeting_ready_status": "review",
                                         "meeting_ready_reason": "invalid_queued_event",
                                         "meeting_ready_updated_at": now})
                return None
            if not permitted:
                # Preserve unknown eligibility for review, but never send old data.
                candidate = dict(data)
                candidate.pop("meeting_ready_event", None)
                assessment = outbox_changes(key, candidate, event["brief_file_id"],
                                            data.get("analysis_payload") or {}, config, now)
                excluded = assessment.get("meeting_ready_status") == "excluded"
                transaction.update(ref, {
                    "meeting_ready_status": "suppressed" if excluded else "review",
                    "meeting_ready_reason": "pre_cutover" if excluded else "ineligible_queued_event",
                    "meeting_ready_updated_at": now})
                return None
            attempts = int(data.get("meeting_ready_attempts", 0)) + 1
            transaction.update(ref, {"meeting_ready_status": "sending",
                                     "meeting_ready_claim_id": claim_id,
                                     "meeting_ready_lease_until": now + LEASE,
                                     "meeting_ready_attempts": attempts,
                                     "meeting_ready_updated_at": now})
            return event, claim_id, attempts

        return claim_transaction(self.client.transaction())

    def finish(self, key, claim_id, delivered, attempts, result):
        """A late sender cannot overwrite a newer claim or successful delivery."""
        ref = self.collection.document(key)
        now = _now()
        delay = min(60 * (2 ** min(max(attempts - 1, 0), 6)), 3600)

        @firestore.transactional
        def finish_transaction(transaction):
            snapshot = ref.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            if (data.get("meeting_ready_status") != "sending" or
                    data.get("meeting_ready_claim_id") != claim_id):
                return False
            changes = {"meeting_ready_status": "delivered" if delivered else "pending",
                       "meeting_ready_lease_until": now,
                       "meeting_ready_updated_at": now,
                       "meeting_ready_last_result": result}
            if delivered:
                changes["meeting_ready_delivered_at"] = now
            else:
                changes["meeting_ready_next_attempt_at"] = now + timedelta(seconds=delay)
            transaction.update(ref, changes)
            return True

        return finish_transaction(self.client.transaction())


def _id_token(config):
    """Obtain a Google ID token and fail closed on an accidental wrong identity."""
    request = functools.partial(Request(), timeout=20)
    token = id_token.fetch_id_token(request, config.url)
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, IndexError, TypeError) as error:
        raise ValueError("Meeting-ready token claims unavailable") from error
    # The receiver verifies the signature; these checks only prevent misconfiguration.
    if claims.get("aud") != config.url or claims.get("email") != config.service_account_email:
        raise ValueError("Meeting-ready token identity or audience mismatch")
    return token


def deliver(event, config, token_factory=_id_token, post=requests.post):
    """Send only the immutable event, and acknowledge strictly on a 2xx reply."""
    validate_event(event)
    token = token_factory(config)
    body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    response = post(config.url, data=body, headers={"Authorization": "Bearer " + token,
                    "Content-Type": "application/json"}, timeout=(5, 25), allow_redirects=False)
    return response.status_code


def run_once(outbox, config, batch_size=MAX_BATCH, deliver_event=deliver):
    """Retry pending events without touching analysis, Drive, or Chat state."""
    if config is None:
        return 0
    reconciled = 0
    for key in outbox.awaiting_brief():
        if reconciled >= batch_size:
            break
        if outbox.reconcile(key, config):
            reconciled += 1
    attempted = 0
    for key in outbox.candidates():
        if attempted >= batch_size:
            break
        claim = outbox.claim(key, config)
        if not claim:
            continue
        event, claim_id, attempts = claim
        attempted += 1
        try:
            status = deliver_event(event, config)
            success = 200 <= status < 300
            result = f"http_{status}"
        except Exception:
            # Never log request contents, ID tokens, or exception details.
            LOGGER.warning("Meeting-ready delivery attempt failed")
            success, result = False, "request_error"
        outbox.finish(key, claim_id, success, attempts, result)
    return attempted


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Deliver or reconcile meeting-ready events")
    parser.add_argument("--reconcile", metavar="SOURCE_KEY")
    parser.add_argument("--verified-start", help="Verified meeting start, RFC3339 with timezone")
    parser.add_argument("--transcript-file-id")
    parser.add_argument("--brief-file-id")
    args = parser.parse_args()
    if not args.reconcile and any((args.verified_start, args.transcript_file_id, args.brief_file_id)):
        parser.error("Correction options require --reconcile")
    try:
        config = load_config(os.environ)
    except ValueError:
        LOGGER.error("Meeting-ready configuration invalid; delivery is disabled")
        return
    if config is None:
        return
    _, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    outbox = MeetingReadyOutbox(os.getenv("GOOGLE_CLOUD_PROJECT") or project)
    if args.reconcile:
        try:
            start = (datetime.fromisoformat(args.verified_start.replace("Z", "+00:00"))
                     if args.verified_start else None)
            status = outbox.reconcile(args.reconcile, config, manual=True, verified_start=start,
                                      transcript_file_id=args.transcript_file_id,
                                      brief_file_id=args.brief_file_id)
        except ValueError:
            parser.error("Invalid reconciliation input")
        LOGGER.info("Meeting-ready reconciliation status: %s", status or "not eligible for review")
        return
    count = run_once(outbox, config)
    LOGGER.info("Meeting-ready worker attempted %d event(s)", count)


if __name__ == "__main__":
    main()
