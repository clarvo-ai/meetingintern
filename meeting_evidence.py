"""Read participant evidence without confusing speakers, invitees, and attendees."""

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


LOGGER = logging.getLogger(__name__)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
TRANSCRIPT_HEADING = re.compile(r"(?im)^.*? - Transcript\s*$")
SPEAKER_LINE = re.compile(r"^([^:\r\n]{2,70}):[ \t]+(.+)$")
TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$", re.I)


def parse_team_roster(raw):
    """Require explicit current identities; an empty roster never proves a team meeting."""
    if not raw:
        return {}
    items = json.loads(raw)
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str)
        and isinstance(item.get("email"), str) and "@" in item["email"]
        for item in items
    ):
        raise ValueError("MEETING_TEAM_ROSTER_JSON must list names and emails")
    roster = {item["email"].strip().casefold(): item["name"].strip() for item in items}
    if len(roster) != len(items) or not all(roster.values()):
        raise ValueError("Meeting team roster has duplicate emails or empty names")
    return roster


def _name_key(name):
    folded = unicodedata.normalize("NFKD", name.casefold())
    return " ".join("".join(char for char in folded if not unicodedata.combining(char)).split())


def parse_meet_documents(documents):
    """Extract labels only from transcript sections, never Gemini note summaries."""
    speaker_turns = {}
    attendees = set()
    speech_words = 0
    for name, content in documents:
        headings = list(TRANSCRIPT_HEADING.finditer(content))
        if headings:
            section = content[headings[-1].start():]
        elif "Transcript" in name and "Notes by Gemini" not in name:
            section = content
        else:
            continue
        lines = [line.strip() for line in section.splitlines()]
        in_attendees = False
        for line in lines:
            if not line:
                continue
            if line.casefold() == "attendees":
                in_attendees = True
                continue
            if in_attendees:
                if line.casefold() == "transcript":
                    in_attendees = False
                else:
                    attendees.update(part.strip() for part in line.split(",") if part.strip())
                continue
            if (TRANSCRIPT_HEADING.fullmatch(line) or line.casefold() == "transcript"
                    or TIMESTAMP.fullmatch(line)
                    or re.match(r"(?i)^Transcription (?:started|ended)(?: after)?\b", line)
                    or line.startswith("This editable transcript was computer generated")):
                continue
            match = SPEAKER_LINE.match(line)
            if match:
                speaker = match.group(1).strip()
                speaker_turns[speaker] = speaker_turns.get(speaker, 0) + 1
                line = match.group(2)
            speech_words += len(line.split())
    return {
        "speakers": sorted(speaker_turns),
        "speaker_turns": speaker_turns,
        "attendees_in_meet_doc": sorted(attendees),
        "source_speech_words": speech_words,
    }


def archived_speech_body(text):
    """Remove the archive provenance header before summarizing spoken content."""
    marker = re.search(r"(?m)^Method: [^\r\n]*\r?\n(?:\r?\n)+", text)
    return text[marker.end():].strip() if marker else text.strip()


def source_evidence(drive, doc_ids):
    """Read all source Docs so a separate Gemini Notes file cannot hide labels."""
    documents = []
    created_at = None
    for doc_id in dict.fromkeys(doc_ids):
        try:
            metadata = drive.files().get(
                fileId=doc_id, fields="id,name,createdTime", supportsAllDrives=True,
            ).execute()
            created_at = created_at or metadata.get("createdTime")
            raw = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
            documents.append((metadata["name"], raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw))
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            LOGGER.info("Meet source Doc %s is no longer readable: HTTP %s", doc_id, error.resp.status)
    evidence = parse_meet_documents(documents)
    evidence["source_doc_created_at"] = created_at
    return evidence


def calendar_invitees(calendar, calendar_ids, doc_ids, created_at):
    """Match only exact Meet attachment IDs; a similar title is insufficient."""
    if not calendar or not calendar_ids or not created_at:
        return {"calendar_match": "unavailable", "calendar_invitees": []}
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    start = (created - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    end = (created + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    expected = set(doc_ids)
    invitees = set()
    matched = False
    accessible = False
    unavailable = False
    for calendar_id in calendar_ids:
        page_token = None
        try:
            while True:
                page = calendar.events().list(
                    calendarId=calendar_id, timeMin=start, timeMax=end,
                    singleEvents=True, maxResults=250, pageToken=page_token,
                    fields="nextPageToken,items(id,attachments(fileId,fileUrl),attendees(email,resource),organizer(email))",
                ).execute()
                accessible = True
                for event in page.get("items", []):
                    ids = set()
                    for item in event.get("attachments", []):
                        if item.get("fileId"):
                            ids.add(item["fileId"])
                        path = urlparse(item.get("fileUrl", "")).path
                        match = re.search(r"/d/([^/]+)", path)
                        if match:
                            ids.add(match.group(1))
                    if not expected.intersection(ids):
                        continue
                    matched = True
                    invitees.update(item["email"].casefold() for item in event.get("attendees", [])
                                    if item.get("email") and not item.get("resource"))
                    if event.get("organizer", {}).get("email"):
                        invitees.add(event["organizer"]["email"].casefold())
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            unavailable = True
            LOGGER.info("Calendar %s is unavailable for meeting evidence: HTTP %s",
                        calendar_id, error.resp.status)
    return {
        "calendar_match": "matched" if matched else "unavailable" if unavailable or not accessible else "not_found",
        "calendar_invitees": sorted(invitees),
    }


def calendar_client(calendar_ids):
    """Calendar access is optional until the Cloud Run identity receives it."""
    if not calendar_ids:
        return None
    credentials, _ = google.auth.default(scopes=[CALENDAR_SCOPE])
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def apply_classification_evidence(payload, evidence, roster):
    """Publish a type only when independently observed identities support it."""
    result = dict(payload)
    proposed = result["meeting_type"]
    result["model_meeting_type"] = proposed
    result["topic_tags"] = [tag for tag in result["topic_tags"] if tag != "other"]
    result["meeting_evidence"] = dict(evidence)
    names = {_name_key(name) for name in roster.values()}
    known_emails = set(roster)
    speakers = evidence.get("speakers", [])
    doc_attendees = evidence.get("attendees_in_meet_doc", [])
    invitees = evidence.get("calendar_invitees", [])
    known_speakers = [name for name in speakers if _name_key(name) in names]
    nonteam_speakers = [name for name in speakers if _name_key(name) not in names]
    nonteam_attendees = [name for name in doc_attendees if _name_key(name) not in names]
    outside_invitees = [email for email in invitees if email.casefold() not in known_emails]
    named_outsiders = [name for name in [*nonteam_speakers, *nonteam_attendees]
                      if len(name.split()) >= 2 and not re.match(r"(?i)^(speaker|person|participant)\s+\d+$", name)]
    substantial = evidence.get("archived_speech_words", 0) >= 50
    team_supported = (
        bool(roster) and substantial and len(known_speakers) >= 2
        and sum(evidence.get("speaker_turns", {}).values()) >= 5
        and not nonteam_speakers and not nonteam_attendees and not outside_invitees
    )
    # An invitation may be a no-show; it only vetoes a team label.
    outside_supported = bool(roster) and substantial and bool(named_outsiders)
    if proposed == "team_internal" and team_supported:
        result["classification_confidence"] = "high"
        result["meeting_evidence"]["type_supported"] = True
    elif (proposed not in ("team_internal", "unknown") and outside_supported
          and result["classification_confidence"] == "high"):
        result["meeting_evidence"]["type_supported"] = True
    else:
        result["meeting_type"] = "unknown"
        result["classification_confidence"] = "low"
        result["meeting_evidence"]["type_supported"] = False
    return result
