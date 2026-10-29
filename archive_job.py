"""One Cloud Run Job execution processes one durably claimed meeting."""

import logging
import os

import google.auth
from googleapiclient.discovery import build

from archive_state import ArchiveState
from audio_archive import (AudioGemini, drive_credentials, existing_archive_doc_id,
                           find_meetings, find_meetings_across_roots,
                           owner_drive_credentials, process_meeting)
from meet_archive_discovery import meeting_from_record
from meeting_auto_artifacts import calendar_ids
from meeting_evidence import meet_client_for_user


LOGGER = logging.getLogger(__name__)


def work_once(source_key, destination_folder_id, api_key, state, credentials,
              service_account_email=None, owners=()):
    """Re-check readiness at job start, then publish or release the claim."""
    data = state.claim_for_work(source_key)
    if not data:
        LOGGER.info("Meeting %s is already claimed or finished", source_key)
        return None
    try:
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
        # Drive metadata can change the computed key between dispatch and work;
        # the saved Doc IDs still identify the same meeting occurrence.
        source_ids = set(data.get("source_context_doc_ids") or ())
        if data.get("source_notes_id"):
            source_ids.add(data["source_notes_id"])
        source_drives = {}
        source_credentials = {}

        def owner_credentials(owner):
            if owner not in source_credentials:
                source_credentials[owner] = owner_drive_credentials(
                    service_account_email, owner)
            return source_credentials[owner]

        def owner_drive(owner):
            if owner not in source_drives:
                source_drives[owner] = build(
                    "drive", "v3", credentials=owner_credentials(owner),
                    cache_discovery=False,
                )
            return source_drives[owner]
        root_ids = data.get("source_root_ids")
        if data.get("source_conference_record"):
            if not service_account_email or not owners:
                raise ValueError("Meet archive source requires delegated service account and owners")
            reader = data.get("meet_reader_email") or data["source_owner_email"]
            meet = meet_client_for_user(service_account_email, reader)
            record = meet.conferenceRecords().get(
                name=data["source_conference_record"]).execute()
            meeting = meeting_from_record(
                record, meet, owner_drive, list(owners),
                preferred=[reader, data["source_owner_email"]],
            )
            meetings = [meeting] if meeting else []
        elif root_ids:
            meetings = find_meetings_across_roots(drive, root_ids)
        else:
            folder = drive.files().get(
                fileId=data["meeting_folder_id"], fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
            meetings = find_meetings(drive, folder)
        meeting = next((item for item in meetings
                        if item.source_key == source_key or
                        (source_ids and source_ids.intersection(item.context_doc_ids))), None)
        if not meeting:
            state.mark_retry(source_key, "Source files are no longer settled or accessible", "processing")
            return None
        existing = existing_archive_doc_id(drive, destination_folder_id, meeting)
        if existing:
            state.mark_done(source_key, existing)
            return existing
        gemini = AudioGemini(api_key, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        if meeting.source_conference_record:
            output = process_meeting(
                drive, docs, credentials, gemini, destination_folder_id, meeting,
                source_drive=owner_drive(meeting.source_owner_email),
                recording_credentials=(owner_credentials(meeting.recording_owner_email)
                                       if meeting.recording_owner_email else None),
            )
        else:
            output = process_meeting(drive, docs, credentials, gemini,
                                     destination_folder_id, meeting)
        state.mark_done(source_key, output)
        return output
    except Exception as error:
        state.mark_retry(source_key, error, "processing")
        raise


def main():
    logging.basicConfig(level=logging.INFO)
    source_key = os.getenv("ARCHIVE_STATE_ID")
    destination = os.getenv("ARCHIVE_DESTINATION_FOLDER_ID")
    api_key = os.getenv("GOOGLE_AI_API_KEY")
    if not source_key or not destination or not api_key:
        raise ValueError("Missing archive worker configuration")
    _, project_id = google.auth.default()
    state = ArchiveState(os.getenv("GOOGLE_CLOUD_PROJECT") or project_id)
    service_account_email = os.getenv("MEETING_MEET_SERVICE_ACCOUNT_EMAIL")
    owners = calendar_ids(os.getenv("MEETING_CALENDAR_IDS", "")) if service_account_email else ()
    output = work_once(source_key, destination, api_key, state, drive_credentials(),
                       service_account_email, owners)
    LOGGER.info("Finished archive work for %s; output Doc: %s", source_key, output)


if __name__ == "__main__":
    main()
