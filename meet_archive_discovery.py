"""Discover completed Meet artifacts by API file ID, independent of Drive folders."""

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from audio_archive import (DOC_MIME, RECORDING_MIME, RECORDING_WAIT, SETTLE_WAIT,
                           Meeting, export_transcript)


LOGGER = logging.getLogger(__name__)
LOOKBACK = timedelta(days=14)
FILE_FIELDS = ("id,name,mimeType,createdTime,modifiedTime,parents,"
               "owners(emailAddress),videoMediaMetadata(durationMillis)")


def _pages(request_factory, key):
    page_token = None
    while True:
        response = request_factory(page_token).execute()
        yield from response.get(key, [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _timestamp(raw):
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _owned_file(drive_factory, file_id, preferred, owners, mime_type):
    """Resolve an exact Drive ID and require an allowlisted actual file owner."""
    for subject in dict.fromkeys([*preferred, *owners]):
        try:
            item = drive_factory(subject).files().get(
                fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True,
            ).execute()
        except HttpError as error:
            if error.resp.status in (403, 404):
                continue
            raise
        if item.get("mimeType") != mime_type:
            return None
        actual = {person.get("emailAddress", "").casefold()
                  for person in item.get("owners", [])}
        if actual.intersection(owners):
            item["source_owner_email"] = sorted(actual.intersection(owners))[0]
            return item
        return None
    return None


def meeting_from_record(record, meet, drive_factory, owners, preferred=(), now=None,
                        not_before=None):
    """Pair exact artifacts; transcribe a settled recording when Meet made no Doc."""
    now = now or datetime.now(timezone.utc)
    if not record.get("endTime"):
        return None
    name = record["name"]
    raw_transcripts = list(_pages(
        lambda token: meet.conferenceRecords().transcripts().list(
            parent=name, pageSize=100, pageToken=token), "transcripts"))
    transcript_artifacts = [item for item in raw_transcripts
        if item.get("state") == "FILE_GENERATED"
        and item.get("docsDestination", {}).get("document")]
    if len(transcript_artifacts) > 1:
        LOGGER.warning("Multiple transcripts for %s; leaving folder fallback to handle it", name)
        return None
    notes_id = (transcript_artifacts[0]["docsDestination"]["document"]
                if transcript_artifacts else None)
    notes = _owned_file(drive_factory, notes_id, preferred, owners, DOC_MIME) if notes_id else None
    if notes_id and not notes:
        LOGGER.info("Meet Doc %s is not owned by an allowed source user or is inaccessible", notes_id)
        return None
    if notes and now - _timestamp(notes["modifiedTime"]) < SETTLE_WAIT:
        return None

    recording_artifacts = list(_pages(
        lambda token: meet.conferenceRecords().recordings().list(
            parent=name, pageSize=100, pageToken=token), "recordings"))
    recordings = [item for item in recording_artifacts
        if item.get("state") == "FILE_GENERATED"
        and item.get("driveDestination", {}).get("file")]
    if len(recordings) > 1:
        LOGGER.warning("Multiple recordings for %s; leaving folder fallback to handle it", name)
        return None
    recording = None
    if recordings:
        recording_id = recordings[0]["driveDestination"]["file"]
        recording = _owned_file(drive_factory, recording_id,
                                [notes["source_owner_email"], *preferred] if notes else preferred,
                                owners,
                                RECORDING_MIME)
        if not recording:
            # A generated recording must be readable before the Doc-only fallback.
            LOGGER.warning("Generated Meet recording %s is not readable by source users", recording_id)
            return None
        if now - _timestamp(recording["modifiedTime"]) < SETTLE_WAIT:
            return None
    elif not notes or now - _timestamp(notes["createdTime"]) < RECORDING_WAIT:
        return None

    if not_before and all(_timestamp(item["createdTime"]) < not_before
                          for item in (notes, recording) if item):
        return None
    if recording and not notes and now - _timestamp(recording["createdTime"]) < RECORDING_WAIT:
        # Meet can generate the video before the transcript Doc.
        return None

    title = re.sub(r"\s+-\s+(?:Recording|Transcript|Notes by Gemini)$", "",
                   (notes or recording)["name"], flags=re.I)
    meeting = Meeting(
        source_key=hashlib.sha256(f"meet:{name}".encode()).hexdigest(),
        folder_id=((notes or recording).get("parents") or [""])[0], folder_name=title,
        recording_id=recording["id"] if recording else None,
        recording_name=recording["name"] if recording else None,
        duration_seconds=(int(recording["videoMediaMetadata"]["durationMillis"]) / 1000
                          if recording and recording.get("videoMediaMetadata", {}).get("durationMillis")
                          else None),
        notes_id=notes_id, notes_name=notes["name"] if notes else "",
        context_doc_ids=(notes_id,) if notes_id else (),
        recording_folder_id=(recording.get("parents") or [None])[0] if recording else None,
        source_owner_email=(notes or recording)["source_owner_email"],
        recording_owner_email=recording["source_owner_email"] if recording else None,
        source_conference_record=name,
        meet_reader_email=preferred[0] if preferred else (notes or recording)["source_owner_email"],
        started_at=_timestamp(record.get("startTime") or (notes or recording)["createdTime"]),
    )
    if not recording and not export_transcript(drive_factory(meeting.source_owner_email), meeting):
        return None
    return meeting


def find_meet_meetings(meet_factory, drive_factory, owners, now=None,
                       not_before=None, errors=None, file_owners=None):
    """Union visible conferences, then resolve actual Drive ownership once."""
    now = now or datetime.now(timezone.utc)
    errors = errors if errors is not None else []
    lower = max(now - LOOKBACK, not_before) if not_before else now - LOOKBACK
    filter_text = f'start_time >= "{lower.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}"'
    records = {}
    clients = {}
    for owner in owners:
        try:
            meet = clients.setdefault(owner, meet_factory(owner))
            for record in _pages(lambda token: meet.conferenceRecords().list(
                    filter=filter_text, pageSize=250, pageToken=token), "conferenceRecords"):
                if record.get("endTime"):
                    entry = records.setdefault(record["name"], [record, []])
                    entry[1].append(owner)
        except Exception:
            LOGGER.exception("Could not list Meet conferences for %s", owner)
            errors.append(f"list:{owner}")
            continue
    for record, preferred in records.values():
        try:
            yield meeting_from_record(record, clients[preferred[0]], drive_factory,
                                      file_owners or owners, preferred, now, not_before)
        except Exception:
            LOGGER.exception("Could not inspect Meet artifacts for %s", record["name"])
            errors.append(f"artifacts:{record['name']}")
            continue
