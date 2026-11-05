"""Configuration and immutable payload for the customer-context outbox."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone


WEBHOOK_URL = "https://ci.clarvo.ai/webhooks/meeting-ready"
_SOURCE_KEY = re.compile(r"[0-9a-f]{64}\Z")
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_SERVICE_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9.-]*\.iam\.gserviceaccount\.com\Z")
EVENT_FIELDS = {"schema_version", "event_id", "source", "meeting_id",
                "transcript_file_id", "brief_file_id"}


@dataclass(frozen=True)
class MeetingReadyConfig:
    url: str
    cutover: datetime
    service_account_email: str


def load_config(env):
    """Stay disabled until all settings are explicit; reject unsafe targets."""
    url = env.get("MEETING_READY_WEBHOOK_URL", "").strip()
    raw_cutover = env.get("MEETING_READY_CUTOVER", "").strip()
    account = env.get("MEETING_READY_SERVICE_ACCOUNT_EMAIL", "").strip()
    if not any((url, raw_cutover, account)):
        return None
    if not all((url, raw_cutover, account)):
        raise ValueError("Meeting-ready configuration is incomplete")
    if url != WEBHOOK_URL or not _SERVICE_ACCOUNT.fullmatch(account):
        raise ValueError("Meeting-ready configuration has an invalid target or service account")
    try:
        cutover = datetime.fromisoformat(raw_cutover.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Meeting-ready cutover must be an RFC3339 timestamp") from error
    if cutover.tzinfo is None or cutover.utcoffset() is None:
        raise ValueError("Meeting-ready cutover must include a timezone")
    return MeetingReadyConfig(url, cutover.astimezone(timezone.utc), account)


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def archive_with_calendar_start(archive, payload):
    """Use an unambiguously matched Calendar event as additional time evidence."""
    evidence = payload.get("meeting_evidence") or {}
    if not isinstance(evidence, dict) or evidence.get("calendar_match") != "matched":
        return archive
    event = evidence.get("calendar_event") or {}
    value = event.get("start") if isinstance(event, dict) else None
    try:
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return archive
    if not _aware(start):
        return archive
    existing = archive.get("meeting_ready_verified_start_at")
    if _aware(existing):
        start = min(start, existing)
    return {**archive, "meeting_ready_verified_start_at": start}


def eligible_archive(archive, config):
    """Admit only meetings newly discovered and started at or after cutover."""
    if config is None:
        return False
    first_seen = archive.get("archive_first_seen_at")
    started = archive.get("meeting_started_at")
    verified = archive.get("meeting_ready_verified_start_at")
    return (archive.get("status") == "done" and _aware(first_seen) and _aware(started)
            and _aware(verified) and first_seen >= config.cutover
            and started >= config.cutover and verified >= config.cutover)


def make_event(source_key, archive, brief_file_id, config):
    if not eligible_archive(archive, config):
        return None
    event = {"schema_version": 1, "event_id": source_key, "source": "meetingintern",
             "meeting_id": source_key, "transcript_file_id": archive.get("output_doc_id"),
             "brief_file_id": brief_file_id}
    validate_event(event)
    return event


def validate_event(event):
    """Reject extra data and invalid IDs rather than leaking it to the receiver."""
    if (not isinstance(event, dict) or set(event) != EVENT_FIELDS or
            type(event.get("schema_version")) is not int or event["schema_version"] != 1 or
            event.get("source") != "meetingintern"):
        raise ValueError("Invalid meeting-ready event shape")
    key = event.get("event_id")
    if not isinstance(key, str) or not _SOURCE_KEY.fullmatch(key) or event.get("meeting_id") != key:
        raise ValueError("Invalid meeting-ready event identity")
    for field in ("transcript_file_id", "brief_file_id"):
        value = event.get(field)
        if not isinstance(value, str) or not _FILE_ID.fullmatch(value):
            raise ValueError("Invalid meeting-ready file ID")
