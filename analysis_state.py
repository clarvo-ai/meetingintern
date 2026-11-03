"""Independent Firestore claims for meeting briefs and Chat delivery."""

from datetime import datetime, timedelta, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter


DISPATCH_LEASE = timedelta(minutes=10)
WORK_LEASE = timedelta(hours=2)
RETRY_DELAY = timedelta(minutes=15)
ACTIVE_STATUSES = ("dispatching", "queued", "processing")


def _now():
    return datetime.now(timezone.utc)


class AnalysisState:
    def __init__(self, project_id):
        self.client = firestore.Client(project=project_id)
        self.collection = self.client.collection("meeting_audio_archive")

    def candidates(self):
        """Visit the oldest ready meeting first so Chat delivery is serialized."""
        query = self.collection.where(filter=FieldFilter(
            "analysis_status", "in", ["pending", *ACTIVE_STATUSES]
        ))
        snapshots = list(query.stream())
        # Older documents lack a meeting timestamp; their archive time is the
        # best available ordering key until they have all been reconciled.
        snapshots.sort(key=lambda item: (
            item.to_dict().get("meeting_started_at") or
            item.to_dict().get("updated_at") or datetime.max.replace(tzinfo=timezone.utc),
            item.id,
        ))
        return [snapshot.id for snapshot in snapshots]

    def reserve_for_dispatch(self, source_key):
        """Prevent two checker runs from launching the same analysis."""
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def reserve(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict()
            if data.get("status") != "done" or not data.get("output_doc_id"):
                return False
            status = data.get("analysis_status")
            if status not in ("pending", *ACTIVE_STATUSES):
                return False
            if status in ACTIVE_STATUSES and data.get("analysis_lease_until", now) > now:
                return False
            if data.get("analysis_next_attempt_at", now) > now:
                return False
            transaction.update(ref, {
                "analysis_status": "dispatching",
                "analysis_lease_until": now + DISPATCH_LEASE,
                "analysis_updated_at": now,
            })
            return True

        return reserve(self.client.transaction())

    def mark_queued(self, source_key, execution_name):
        """A fast worker claim must not be overwritten by the checker."""
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def update(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and snapshot.to_dict().get("analysis_status") == "dispatching":
                transaction.update(ref, {
                    "analysis_status": "queued",
                    "analysis_lease_until": now + DISPATCH_LEASE,
                    "analysis_execution_name": execution_name,
                    "analysis_updated_at": now,
                })

        update(self.client.transaction())

    def claim_for_work(self, source_key):
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def claim(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            status = data.get("analysis_status")
            if data.get("status") != "done" or status not in ("pending", *ACTIVE_STATUSES):
                return None
            if status == "processing" and data.get("analysis_lease_until", now) > now:
                return None
            if data.get("analysis_next_attempt_at", now) > now:
                return None
            transaction.update(ref, {
                "analysis_status": "processing",
                "analysis_lease_until": now + WORK_LEASE,
                "analysis_updated_at": now,
            })
            return data

        return claim(self.client.transaction())

    def save_brief(self, source_key, brief_doc_id, payload, chat_text):
        """Persist exactly what will be posted before trying the webhook."""
        self.collection.document(source_key).update({
            "analysis_brief_doc_id": brief_doc_id,
            "analysis_payload": payload,
            "analysis_meeting_type": payload["meeting_type"],
            "analysis_topic_tags": payload["topic_tags"],
            "analysis_chat_text": chat_text,
            "analysis_updated_at": _now(),
        })

    def mark_invalid_source(self, source_key, reason):
        """Permanently suppress a brief when the archived Doc has no speech."""
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def invalidate(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and snapshot.to_dict().get("analysis_status") == "processing":
                transaction.update(ref, {
                    "analysis_status": "invalid_source",
                    "analysis_meeting_type": "unknown",
                    "analysis_topic_tags": [],
                    "analysis_invalid_reason": reason,
                    "analysis_lease_until": now,
                    "analysis_updated_at": now,
                })

        invalidate(self.client.transaction())

    def mark_sending(self, source_key):
        """Fence off automatic retries before a non-idempotent webhook call."""
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def sending(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or snapshot.to_dict().get("analysis_status") != "processing":
                raise RuntimeError("Analysis is no longer claimed for Chat delivery")
            transaction.update(ref, {
                "analysis_status": "sending",
                "analysis_chat_attempted_at": now,
                "analysis_updated_at": now,
            })

        sending(self.client.transaction())

    def mark_posted(self, source_key, message_name):
        now = _now()
        self.collection.document(source_key).update({
            "analysis_status": "posted",
            "analysis_chat_message_name": message_name,
            "analysis_lease_until": now,
            "analysis_updated_at": now,
        })

    def mark_delivery_unknown(self, source_key, reason):
        """Keep uncertain webhook calls out of the automatic retry queue."""
        self.collection.document(source_key).update({
            "analysis_status": "delivery_unknown",
            "analysis_last_error": str(reason)[:300],
            "analysis_updated_at": _now(),
        })

    def mark_retry(self, source_key, reason, expected_status):
        """Release only the failed claim, preserving any completed brief."""
        ref = self.collection.document(source_key)
        now = _now()

        @firestore.transactional
        def release(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and snapshot.to_dict().get("analysis_status") == expected_status:
                transaction.update(ref, {
                    "analysis_status": "pending",
                    "analysis_lease_until": now,
                    "analysis_next_attempt_at": now + RETRY_DELAY,
                    "analysis_last_error": str(reason)[:300],
                    "analysis_updated_at": now,
                })

        release(self.client.transaction())
