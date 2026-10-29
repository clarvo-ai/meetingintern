"""Read participant evidence without confusing speakers, invitees, and attendees."""

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


LOGGER = logging.getLogger(__name__)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
MEET_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
TRANSCRIPT_HEADING = re.compile(r"(?im)^.*? - Transcript\s*$")
SPEAKER_LINE = re.compile(r"^([^:\r\n]{2,70}):[ \t]+(.+)$")
TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$", re.I)
MEET_CODE = re.compile(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b", re.I)
SOURCE_TIME = re.compile(
    r"(20\d{2})[/-](\d{2})[/-](\d{2})\s+(\d{2}):(\d{2})\s*"
    r"(EEST|EET|GMT[+-]\d{1,2}|UTC[+-]\d{1,2})\b", re.I,
)
CODE_TIME_TOLERANCE = timedelta(minutes=90)
MEET_SOURCE_TIME_TOLERANCE = timedelta(minutes=15)


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


def _source_time(name):
    """Read the meeting's named start time, not Drive's later upload timestamp."""
    match = SOURCE_TIME.search(name or "")
    if not match:
        return None
    year, month, day, hour, minute, zone = match.groups()
    zone = zone.upper()
    offset = (3 if zone == "EEST" else 2) if zone in ("EEST", "EET") else int(zone[3:])
    moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                      tzinfo=timezone(timedelta(hours=offset)))
    return moment.astimezone(timezone.utc).isoformat()


def _source_artifact(metadata):
    """Retain ID and owner identity without depending on its Drive parent folder."""
    name = metadata.get("name", "")
    urls = re.findall(r"https?://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}",
                      metadata.get("description", ""), re.I)
    codes = {match.group().lower() for match in MEET_CODE.finditer(name)}
    codes.update(match.group().lower() for url in urls for match in MEET_CODE.finditer(url))
    return {
        "id": metadata["id"],
        "name": name,
        "mime_type": metadata.get("mimeType"),
        "created_at": metadata.get("createdTime"),
        "meeting_at": _source_time(name),
        "owner_emails": sorted({person["emailAddress"].casefold()
                                for person in metadata.get("owners", [])
                                if person.get("emailAddress")}),
        "meet_codes": sorted(codes),
    }


def source_evidence(drive, doc_ids, recording_id=None, recording_drive=None):
    """Read source artifact IDs directly so separate Meet folders do not matter."""
    recording_drive = recording_drive or drive
    documents = []
    created_at = None
    artifacts = []
    for doc_id in dict.fromkeys(doc_ids):
        try:
            metadata = drive.files().get(
                fileId=doc_id,
                fields="id,name,mimeType,createdTime,owners(emailAddress),description",
                supportsAllDrives=True,
            ).execute()
            created_at = created_at or metadata.get("createdTime")
            artifacts.append(_source_artifact(metadata))
            raw = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
            documents.append((metadata["name"], raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw))
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            LOGGER.info("Meet source Doc %s is no longer readable: HTTP %s", doc_id, error.resp.status)
    if recording_id:
        try:
            metadata = recording_drive.files().get(
                fileId=recording_id,
                fields="id,name,mimeType,createdTime,owners(emailAddress),description",
                supportsAllDrives=True,
            ).execute()
            artifacts.append(_source_artifact(metadata))
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            LOGGER.info("Meet recording %s is no longer readable: HTTP %s",
                        recording_id, error.resp.status)
    evidence = parse_meet_documents(documents)
    evidence["source_doc_created_at"] = created_at
    evidence["source_artifacts"] = artifacts
    return evidence


def _event_time(event):
    raw = event.get("start", {}).get("dateTime")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


def _event_code(event):
    code = event.get("conferenceData", {}).get("conferenceId", "").lower()
    if MEET_CODE.fullmatch(code):
        return code
    for value in [event.get("hangoutLink", ""),
                  *(item.get("uri", "") for item in event.get("conferenceData", {}).get("entryPoints", []))]:
        match = MEET_CODE.search(urlparse(value).path)
        if match:
            return match.group().lower()
    return None


def _attachment_ids(event):
    ids = set()
    for item in event.get("attachments", []):
        if item.get("fileId"):
            ids.add(item["fileId"])
        match = re.search(r"/d/([^/]+)", urlparse(item.get("fileUrl", "")).path)
        if match:
            ids.add(match.group(1))
    return ids


def _event_identity(event):
    """Copies on multiple invitee calendars are one event, not ambiguity."""
    return (event.get("iCalUID") or event.get("id"),
            event.get("organizer", {}).get("email", "").casefold(),
            event.get("start", {}).get("dateTime"))


def _calendar_event_details(event):
    attendees = sorted(
        ({"email": item["email"].casefold(),
          "name": item.get("displayName"),
          "response_status": item.get("responseStatus", "unknown")}
         for item in event.get("attendees", [])
         if item.get("email") and not item.get("resource")),
        key=lambda item: item["email"],
    )
    organizer = event.get("organizer", {}).get("email", "").casefold() or None
    meet_url = event.get("hangoutLink") or next(
        (item.get("uri") for item in event.get("conferenceData", {}).get("entryPoints", [])
         if item.get("entryPointType") == "video"), None,
    )
    return {
        "id": event.get("id"),
        "title": event.get("summary"),
        "organizer_email": organizer,
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
        "time_zone": event.get("start", {}).get("timeZone"),
        "event_url": event.get("htmlLink"),
        "meet_url": meet_url,
        "meet_code": _event_code(event),
        "invitees": attendees,
    }


def resolve_speaker_identities(evidence, roster):
    """Link a transcript label only to a unique full name from known identities."""
    people = {}
    for email, name in roster.items():
        people.setdefault(_name_key(name), set()).add(email.casefold())
    event = evidence.get("calendar_event") or {}
    for invitee in event.get("invitees", []):
        if invitee.get("name"):
            people.setdefault(_name_key(invitee["name"]), set()).add(invitee["email"])
    meet_people = {}
    for participant in evidence.get("confirmed_meet_participants", []):
        if participant.get("identity_kind") == "signed_in" and participant.get("display_name"):
            meet_people.setdefault(_name_key(participant["display_name"]), set()).add(
                participant["user_id"])
    resolved = []
    for speaker in evidence.get("speakers", []):
        emails = people.get(_name_key(speaker), set())
        users = meet_people.get(_name_key(speaker), set())
        if len(emails) <= 1 and len(users) <= 1 and (emails or users):
            person = {"speaker": speaker}
            if emails:
                person["email"] = next(iter(emails))
            if users:
                person["meet_user_id"] = next(iter(users))
            resolved.append(person)
    return {"resolved_speakers": resolved}


def meet_client_for_user(service_account_email, user_email, source_credentials=None):
    """Use keyless domain-wide delegation for the one known source owner."""
    source = source_credentials or google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])[0]
    credentials = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account_email,
        target_scopes=[MEET_SCOPE],
        subject=user_email,
    )
    return build("meet", "v2", credentials=credentials, cache_discovery=False)


def _meet_pages(request_factory, key):
    """Consume every page so a partial API response cannot establish attendance."""
    items = []
    page_token = None
    while True:
        response = request_factory(page_token).execute()
        items.extend(response.get(key, []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


def _meet_anchor(evidence):
    times = [datetime.fromisoformat(item["meeting_at"])
             for item in evidence.get("source_artifacts", []) if item.get("meeting_at")]
    if times:
        return times[0], MEET_SOURCE_TIME_TOLERANCE
    event = evidence.get("calendar_event") or {}
    if event.get("start"):
        return datetime.fromisoformat(event["start"].replace("Z", "+00:00")), CODE_TIME_TOLERANCE
    return None, None


def meet_participant_context(meet_factory, evidence, allowed_user_emails):
    """Match one conference by recording ID or code plus time before trusting joiners."""
    empty = {"meet_participant_record_status": "unavailable",
             "meet_conference_record": None, "meet_record_match_basis": None,
             "confirmed_meet_participants": []}
    if not meet_factory:
        return empty
    artifacts = evidence.get("source_artifacts") or []
    owners = {email for item in artifacts for email in item.get("owner_emails", [])}
    event = evidence.get("calendar_event") if evidence.get("calendar_match") == "matched" else None
    user = (event or {}).get("organizer_email")
    allowed = {email.casefold() for email in allowed_user_emails}
    if user not in owners or user not in allowed:
        users = owners.intersection(allowed)
        user = next(iter(users)) if len(users) == 1 else None
    codes = {code for item in artifacts for code in item.get("meet_codes", [])}
    if event and event.get("meet_code"):
        codes.add(event["meet_code"])
    if not user or len(codes) != 1:
        return empty
    code = next(iter(codes))
    recording_ids = {item["id"] for item in artifacts if item.get("mime_type", "").startswith("video/")}
    anchor, tolerance = _meet_anchor(evidence)
    try:
        meet = meet_factory(user)
        records = _meet_pages(
            lambda token: meet.conferenceRecords().list(
                filter=f'space.meeting_code = "{code}"', pageSize=250,
                pageToken=token), "conferenceRecords")
        exact = []
        if recording_ids:
            for record in records:
                recordings = _meet_pages(
                    lambda token: meet.conferenceRecords().recordings().list(
                        parent=record["name"], pageSize=100, pageToken=token), "recordings")
                if recording_ids.intersection(
                    item.get("driveDestination", {}).get("file") for item in recordings):
                    exact.append(record)
        basis = "recording_id" if exact else "meet_code_time"
        candidates = exact or [
            record for record in records
            if anchor and abs(datetime.fromisoformat(record["startTime"].replace("Z", "+00:00"))
                              - anchor) <= tolerance
        ]
        if len(candidates) > 1:
            return {**empty, "meet_participant_record_status": "ambiguous"}
        if not candidates:
            return {**empty, "meet_participant_record_status": "not_found"}
        record = candidates[0]
        raw = _meet_pages(
            lambda token: meet.conferenceRecords().participants().list(
                parent=record["name"], pageSize=250, pageToken=token), "participants")
    except Exception as error:
        LOGGER.warning("Meet participant evidence unavailable for %s: %s", user, type(error).__name__)
        return empty
    participants = []
    for item in raw:
        signed = item.get("signedinUser") or {}
        anonymous = item.get("anonymousUser") or {}
        phone = item.get("phoneUser") or {}
        kind = "signed_in" if signed else "anonymous" if anonymous else "phone" if phone else "unknown"
        identity = signed or anonymous or phone
        participants.append({
            "display_name": identity.get("displayName"),
            "identity_kind": kind,
            "user_id": signed.get("user"),
            "joined_at": item.get("earliestStartTime"),
            "left_at": item.get("latestEndTime"),
        })
    return {"meet_participant_record_status": "matched",
            "meet_conference_record": record["name"],
            "meet_record_match_basis": basis,
            "confirmed_meet_participants": participants}


def calendar_context(calendar, calendar_ids, evidence):
    """Match source artifact IDs first, then exact Meet code, time, and owner."""
    empty = {"calendar_match": "unavailable", "calendar_match_basis": None,
             "calendar_event": None, "calendar_invitees": []}
    if not calendar or not calendar_ids:
        return empty
    artifacts = evidence.get("source_artifacts") or []
    source_ids = {item["id"] for item in artifacts}
    source_codes = {code for item in artifacts for code in item.get("meet_codes", [])}
    source_owners = {email for item in artifacts for email in item.get("owner_emails", [])}
    source_times = [datetime.fromisoformat(item["meeting_at"])
                    for item in artifacts if item.get("meeting_at")]
    anchor = source_times[0] if source_times else None
    if not anchor and evidence.get("source_doc_created_at"):
        anchor = datetime.fromisoformat(evidence["source_doc_created_at"].replace("Z", "+00:00"))
    if not anchor:
        return empty
    start = (anchor - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    end = (anchor + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    events = {}
    accessible = False
    unavailable = False
    for calendar_id in calendar_ids:
        page_token = None
        try:
            while True:
                page = calendar.events().list(
                    calendarId=calendar_id, timeMin=start, timeMax=end,
                    singleEvents=True, maxResults=250, pageToken=page_token,
                    fields=("nextPageToken,items(id,iCalUID,status,summary,start,end,"
                            "organizer(email),htmlLink,hangoutLink,"
                            "conferenceData(conferenceId,entryPoints(entryPointType,uri)),"
                            "attachments(fileId,fileUrl),"
                            "attendees(email,displayName,responseStatus,resource))"),
                ).execute()
                accessible = True
                for event in page.get("items", []):
                    if event.get("status") == "cancelled" or not _event_time(event):
                        continue
                    identity = _event_identity(event)
                    previous = events.get(identity)
                    if not previous or (len(event.get("attendees", [])) + len(event.get("attachments", []))
                                        > len(previous.get("attendees", [])) + len(previous.get("attachments", []))):
                        events[identity] = event
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            unavailable = True
            LOGGER.info("Calendar %s is unavailable for meeting evidence: HTTP %s",
                        calendar_id, error.resp.status)
    attachments = [event for event in events.values()
                   if source_ids.intersection(_attachment_ids(event))]
    basis = "attachment" if attachments else "meet_code"
    candidates = attachments or [
        event for event in events.values()
        if (_event_code(event) in source_codes
            and event.get("organizer", {}).get("email", "").casefold() in source_owners
            and any(abs(_event_time(event) - moment) <= CODE_TIME_TOLERANCE
                    for moment in source_times))
    ]
    if len(candidates) > 1:
        return {**empty, "calendar_match": "ambiguous"}
    if not candidates:
        return {**empty, "calendar_match": "unavailable" if unavailable or not accessible else "not_found"}
    details = _calendar_event_details(candidates[0])
    return {
        "calendar_match": "matched", "calendar_match_basis": basis,
        "calendar_event": details,
        "calendar_invitees": [item["email"] for item in details["invitees"]],
    }


def calendar_invitees(calendar, calendar_ids, doc_ids, created_at):
    """Compatibility entry point for callers that only know source Doc IDs."""
    return calendar_context(calendar, calendar_ids, {
        "source_doc_created_at": created_at,
        "source_artifacts": [{"id": doc_id} for doc_id in doc_ids],
    })


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
    participants = evidence.get("confirmed_meet_participants", [])
    signed_participants = [item["display_name"] for item in participants
                           if item.get("identity_kind") == "signed_in" and item.get("display_name")]
    outside_joiners = [name for name in signed_participants
                       if _name_key(name) not in names and len(name.split()) >= 2]
    all_joiners_known_team = (bool(participants) and len(signed_participants) == len(participants)
                              and all(_name_key(name) in names for name in signed_participants))
    known_speakers = [name for name in speakers if _name_key(name) in names]
    nonteam_speakers = [name for name in speakers if _name_key(name) not in names]
    nonteam_attendees = [name for name in doc_attendees if _name_key(name) not in names]
    outside_invitees = [email for email in invitees if email.casefold() not in known_emails]
    named_outsiders = [name for name in [*nonteam_speakers, *nonteam_attendees]
                      if len(name.split()) >= 2 and not re.match(r"(?i)^(speaker|person|participant)\s+\d+$", name)]
    substantial = evidence.get("archived_speech_words", 0) >= 50
    team_supported = bool(roster) and substantial and not nonteam_speakers and not nonteam_attendees and (
        (all_joiners_known_team and len(signed_participants) >= 2)
        or (not participants and len(known_speakers) >= 2
            and sum(evidence.get("speaker_turns", {}).values()) >= 5
            and not outside_invitees)
    )
    # Confirmed joiners outrank invitations; an external invitee may not have joined.
    outside_supported = bool(roster) and substantial and bool([*named_outsiders, *outside_joiners])
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
