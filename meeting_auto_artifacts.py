"""Enable Meet recording, transcription, and notes before owned Calendar events."""

import logging
import re
from datetime import datetime, timedelta, timezone

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build


LOGGER = logging.getLogger(__name__)
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"
LOOKAHEAD = timedelta(hours=24)
MEET_CODE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
ARTIFACT_FIELDS = (
    ("recordingConfig", "autoRecordingGeneration"),
    ("transcriptionConfig", "autoTranscriptionGeneration"),
    ("smartNotesConfig", "autoSmartNotesGeneration"),
)


def calendar_ids(raw):
    """The explicit calendar allowlist is also the organizer allowlist."""
    ids = [value.strip().casefold() for value in raw.split(",") if value.strip()]
    if not ids or len(ids) != len(set(ids)) or any("@" not in value for value in ids):
        raise ValueError("MEETING_CALENDAR_IDS must contain unique calendar email addresses")
    return ids


def calendar_client(credentials=None):
    credentials = credentials or google.auth.default(scopes=[CALENDAR_SCOPE])[0]
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def meet_settings_client(service_account_email, organizer_email, source_credentials=None):
    """Use keyless delegation so only the actual organizer changes their space."""
    source = source_credentials or google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )[0]
    delegated = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=service_account_email,
        target_scopes=[MEET_SETTINGS_SCOPE],
        subject=organizer_email,
    )
    return build("meet", "v2", credentials=delegated, cache_discovery=False)


def upcoming_owned_events(calendar, owner_email, now):
    """Expand recurring events and ignore invitations organized by someone else."""
    page_token = None
    while True:
        response = calendar.events().list(
            calendarId=owner_email,
            timeMin=now.isoformat(),
            timeMax=(now + LOOKAHEAD).isoformat(),
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
            fields=("nextPageToken,items(id,status,start(dateTime),organizer(email),"
                    "creator(email),conferenceData(conferenceId,conferenceSolution(key(type))))"),
        ).execute()
        for event in response.get("items", []):
            if event.get("status") != "confirmed":
                continue
            start = event.get("start", {}).get("dateTime")
            if not start or datetime.fromisoformat(start.replace("Z", "+00:00")) <= now:
                continue
            organizer = event.get("organizer", {}).get("email", "").casefold()
            creator = event.get("creator", {}).get("email", "").casefold()
            if organizer != owner_email or (creator and creator != owner_email):
                continue
            conference = event.get("conferenceData", {})
            if conference.get("conferenceSolution", {}).get("key", {}).get("type") != "hangoutsMeet":
                continue
            code = conference.get("conferenceId", "")
            if MEET_CODE.fullmatch(code):
                yield event, code.casefold()
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def enable_upcoming_artifacts(calendar, meet_factory, owners, now=None):
    """Patch only missing artifacts; unrelated Meet settings stay intact."""
    now = now or datetime.now(timezone.utc)
    result = {"eligible_events": 0, "already_on": 0, "updated": 0, "errors": 0}
    for owner in owners:
        seen_codes = set()
        try:
            events = list(upcoming_owned_events(calendar, owner, now))
        except Exception:
            LOGGER.exception("Could not list upcoming Calendar events for %s", owner)
            result["errors"] += 1
            continue
        for event, code in events:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            result["eligible_events"] += 1
            try:
                meet = meet_factory(owner)
                space = meet.spaces().get(name=f"spaces/{code}").execute()
                artifact_config = space.get("config", {}).get("artifactConfig", {})
                missing = [(config, field) for config, field in ARTIFACT_FIELDS
                           if artifact_config.get(config, {}).get(field) != "ON"]
                if not missing:
                    result["already_on"] += 1
                    continue
                mask = ",".join(f"config.artifactConfig.{config}.{field}"
                                for config, field in missing)
                meet.spaces().patch(
                    name=space["name"], updateMask=mask,
                    body={"name": space["name"], "config": {"artifactConfig": {
                        config: {field: "ON"} for config, field in missing
                    }}},
                ).execute()
                result["updated"] += 1
                LOGGER.info("Enabled Meet artifacts for %s event %s", owner, event["id"])
            except Exception:
                LOGGER.exception("Could not enable Meet artifacts for %s event %s", owner,
                                 event.get("id"))
                result["errors"] += 1
    return result
