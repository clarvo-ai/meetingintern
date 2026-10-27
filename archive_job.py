"""One Cloud Run Job execution processes one durably claimed meeting."""

import logging
import os

import google.auth
from googleapiclient.discovery import build

from archive_state import ArchiveState
from audio_archive import (AudioGemini, drive_credentials, existing_archive_doc_id,
                           find_meetings, process_meeting)


LOGGER = logging.getLogger(__name__)


def work_once(source_key, destination_folder_id, api_key, state, credentials):
    """Re-check readiness at job start, then publish or release the claim."""
    data = state.claim_for_work(source_key)
    if not data:
        LOGGER.info("Meeting %s is already claimed or finished", source_key)
        return None
    try:
        drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
        docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
        folder_id = data["meeting_folder_id"]
        folder = drive.files().get(
            fileId=folder_id, fields="id,name,mimeType", supportsAllDrives=True
        ).execute()
        meeting = next((item for item in find_meetings(drive, folder)
                        if item.source_key == source_key), None)
        if not meeting:
            state.mark_retry(source_key, "Source files are no longer settled or accessible", "processing")
            return None
        existing = existing_archive_doc_id(drive, destination_folder_id, meeting)
        if existing:
            state.mark_done(source_key, existing)
            return existing
        gemini = AudioGemini(api_key, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        output = process_meeting(drive, docs, credentials, gemini, destination_folder_id, meeting)
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
    output = work_once(source_key, destination, api_key, state, drive_credentials())
    LOGGER.info("Finished archive work for %s; output Doc: %s", source_key, output)


if __name__ == "__main__":
    main()
