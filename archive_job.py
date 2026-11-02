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
from company_reference import parse_company_references
from meeting_evidence import (calendar_client, calendar_context, meet_client_for_user,
                              parse_team_roster, source_evidence)
from transcript_cleanup import CLEANUP_VERSION, parse_spelling_hints


LOGGER = logging.getLogger(__name__)


def cleanup_hints(meeting, source_drive, recording_drive, calendar, owners,
                  roster=None, company_references=None, spelling_hints=None):
    """Supply directory and uniquely matched event data only as spelling hints."""
    context = {
        "team_roster": list((roster or {}).values()),
        "company_directory": [{"name": item["name"], "aliases": item.get("aliases", [])}
                              for item in (company_references or [])],
        "product_and_tool_names": spelling_hints or [],
    }
    if calendar:
        try:
            evidence = source_evidence(source_drive, list(meeting.context_doc_ids),
                                       meeting.recording_id, recording_drive)
            matched = calendar_context(calendar, owners, evidence)
            context["source_speakers"] = evidence.get("speakers", [])
            if matched.get("calendar_match") == "matched":
                context["calendar_event"] = matched.get("calendar_event")
        except Exception:
            LOGGER.warning("Could not read optional calendar context for transcript cleanup")
    return context


def work_once(source_key, destination_folder_id, api_key, state, credentials,
              service_account_email=None, owners=(), calendar=None, roster=None,
              company_references=None, spelling_hints=None):
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
            metadata = drive.files().get(fileId=existing, fields="properties",
                                         supportsAllDrives=True).execute()
            if (metadata.get("properties") or {}).get("transcript_cleanup_version") != CLEANUP_VERSION:
                state.mark_retry(source_key, "Existing transcript requires cleanup backfill", "processing")
                return None
            state.mark_done(source_key, existing)
            return existing
        source_drive = (owner_drive(meeting.source_owner_email)
                        if meeting.source_conference_record else drive)
        recording_drive = (owner_drive(meeting.recording_owner_email)
                           if meeting.source_conference_record and meeting.recording_owner_email
                           else source_drive)
        cleanup_context = cleanup_hints(meeting, source_drive, recording_drive, calendar,
                                        owners, roster, company_references, spelling_hints)
        gemini = AudioGemini(api_key, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        if meeting.source_conference_record:
            output = process_meeting(
                drive, docs, credentials, gemini, destination_folder_id, meeting,
                source_drive=source_drive,
                recording_credentials=(owner_credentials(meeting.recording_owner_email)
                                       if meeting.recording_owner_email else None),
                cleanup_context=cleanup_context,
            )
        else:
            output = process_meeting(drive, docs, credentials, gemini, destination_folder_id,
                                     meeting, cleanup_context=cleanup_context)
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
    owners = (list(dict.fromkeys([
        *calendar_ids(os.getenv("MEETING_CALENDAR_IDS", "")),
        *calendar_ids(os.getenv("MEETING_ADDITIONAL_FILE_OWNERS", ""), allow_empty=True),
    ])) if service_account_email else ())
    roster = parse_team_roster(os.getenv("MEETING_TEAM_ROSTER_JSON"))
    references = parse_company_references(os.getenv("MEETING_COMPANY_REFERENCE_JSON"))
    spelling_hints = parse_spelling_hints(os.getenv("MEETING_TRANSCRIPT_TERMS_JSON"))
    output = work_once(source_key, destination, api_key, state, drive_credentials(),
                       service_account_email, owners, calendar_client(owners), roster, references,
                       spelling_hints)
    LOGGER.info("Finished archive work for %s; output Doc: %s", source_key, output)


if __name__ == "__main__":
    main()
