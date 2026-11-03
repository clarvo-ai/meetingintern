"""Firestore claims for 15-minute discovery and long-running archive jobs."""

from datetime import datetime, timedelta, timezone

from google.cloud import firestore


DISPATCH_LEASE = timedelta(minutes=10)
WORK_LEASE = timedelta(hours=12)
RETRY_DELAY = timedelta(minutes=15)


def _utc_now():
    return datetime.now(timezone.utc)


def _lease_active(data, now):
    lease = data.get("lease_until")
    return lease is not None and lease > now


class ArchiveState:
    def __init__(self, project_id):
        self.client = firestore.Client(project=project_id)
        self.collection = self.client.collection("meeting_audio_archive")

    def reserve_for_dispatch(self, meeting, root_id, root_ids=None):
        """Atomically reserve a meeting so concurrent checks launch one job."""
        ref = self.collection.document(meeting.source_key)
        now = _utc_now()

        @firestore.transactional
        def reserve(transaction):
            snapshot = ref.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            if data.get("status") in ("done", "skipped"):
                return False
            if data.get("status") in ("dispatching", "queued", "processing") and _lease_active(data, now):
                return False
            if data.get("next_attempt_at") and data["next_attempt_at"] > now:
                return False
            transaction.set(ref, {
                "status": "dispatching",
                "lease_until": now + DISPATCH_LEASE,
                "source_root_id": root_id,
                "source_root_ids": list(root_ids or [root_id]),
                "meeting_folder_id": meeting.folder_id,
                "recording_folder_id": getattr(meeting, "recording_folder_id", None),
                "source_recording_id": getattr(meeting, "recording_id", None),
                "source_notes_id": meeting.notes_id,
                "source_context_doc_ids": list(getattr(meeting, "context_doc_ids", (meeting.notes_id,))),
                "source_owner_email": getattr(meeting, "source_owner_email", None),
                "recording_owner_email": getattr(meeting, "recording_owner_email", None),
                "source_conference_record": getattr(meeting, "source_conference_record", None),
                "meet_reader_email": getattr(meeting, "meet_reader_email", None),
                "meeting_started_at": getattr(meeting, "started_at", None),
                "updated_at": now,
            }, merge=True)
            return True

        return reserve(self.client.transaction())

    def mark_queued(self, source_key, execution_name):
        """Do not regress a worker that claimed the dispatch immediately."""
        ref = self.collection.document(source_key)
        now = _utc_now()

        @firestore.transactional
        def update(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and snapshot.to_dict().get("status") == "dispatching":
                transaction.update(ref, {
                    "status": "queued",
                    "lease_until": now + DISPATCH_LEASE,
                    "execution_name": execution_name,
                    "updated_at": now,
                })

        update(self.client.transaction())

    def claim_for_work(self, source_key):
        """One worker owns the source for at most twelve hours."""
        ref = self.collection.document(source_key)
        now = _utc_now()

        @firestore.transactional
        def claim(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict()
            if data.get("status") in ("done", "skipped"):
                return None
            if data.get("status") == "processing" and _lease_active(data, now):
                return None
            if data.get("next_attempt_at") and data["next_attempt_at"] > now:
                return None
            transaction.update(ref, {
                "status": "processing",
                "lease_until": now + WORK_LEASE,
                "updated_at": now,
            })
            return data

        return claim(self.client.transaction())

    def mark_done(self, source_key, output_doc_id=None):
        """Finish archiving and queue analysis in the same document update."""
        ref = self.collection.document(source_key)
        now = _utc_now()

        @firestore.transactional
        def finish(transaction):
            snapshot = ref.get(transaction=transaction)
            data = snapshot.to_dict() if snapshot.exists else {}
            changes = {
                "status": "done" if output_doc_id else "skipped",
                "output_doc_id": output_doc_id,
                "lease_until": now,
                "updated_at": now,
            }
            if output_doc_id and not data.get("analysis_status"):
                changes["analysis_status"] = "pending"
            transaction.update(ref, changes)

        finish(self.client.transaction())

    def mark_retry(self, source_key, reason, expected_status):
        """Release only the claim that failed, not a newer worker's claim."""
        now = _utc_now()
        ref = self.collection.document(source_key)

        @firestore.transactional
        def release(transaction):
            snapshot = ref.get(transaction=transaction)
            if snapshot.exists and snapshot.to_dict().get("status") == expected_status:
                transaction.update(ref, {
                    "status": "pending",
                    "lease_until": now,
                    "next_attempt_at": now + RETRY_DELAY,
                    "last_error": str(reason)[:300],
                    "updated_at": now,
                })

        release(self.client.transaction())
