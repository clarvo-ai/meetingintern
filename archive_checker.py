"""Fast Cloud Run checker, intended for a 15-minute Cloud Scheduler cadence."""

import logging
import os
import re
from datetime import datetime

import google.auth
from flask import Flask, jsonify
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from archive_state import ArchiveState
from audio_archive import (FOLDER_MIME, drive_credentials, find_archived_recordings,
                           find_meetings, list_children)


LOGGER = logging.getLogger(__name__)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def launch_job(job_name, source_key, session=None):
    """Start one Cloud Run Job execution with a single source key override."""
    if not re.fullmatch(r"projects/[^/]+/locations/[^/]+/jobs/[^/]+", job_name):
        raise ValueError("ARCHIVE_JOB_NAME must be a full Cloud Run Job resource name")
    if session is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
    response = session.post(
        f"https://run.googleapis.com/v2/{job_name}:run",
        json={"overrides": {"containerOverrides": [{"env": [
            {"name": "ARCHIVE_STATE_ID", "value": source_key}
        ]}]}},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("name", "")


def check_once(roots, destination_folder_id, job_name, state, drive, dispatch_limit=5,
               launch=launch_job, not_before=None):
    """Discover only ready meetings and reserve each before launching work."""
    archived = find_archived_recordings(drive, destination_folder_id)
    dispatched = []
    ready_count = 0
    seen_docs = set()
    seen_recordings = set()
    for root_id in roots:
        folders = [item for item in list_children(drive, root_id)
                   if item["mimeType"] == FOLDER_MIME and item["name"] != "Meet Recordings"]
        folders.sort(key=lambda item: item.get("createdTime", ""), reverse=True)
        for folder in folders:
            for meeting in find_meetings(drive, folder, not_before=not_before):
                ready_count += 1
                if (meeting.notes_id in seen_docs or
                        (meeting.recording_id and meeting.recording_id in seen_recordings)):
                    continue
                seen_docs.add(meeting.notes_id)
                if meeting.recording_id:
                    seen_recordings.add(meeting.recording_id)
                if (meeting.source_key in archived or meeting.notes_id in archived or
                        meeting.recording_id in archived):
                    continue
                if len(dispatched) >= dispatch_limit:
                    continue
                if not state.reserve_for_dispatch(meeting, root_id):
                    continue
                try:
                    execution = launch(job_name, meeting.source_key)
                    state.mark_queued(meeting.source_key, execution)
                    dispatched.append(meeting.source_key)
                except Exception as error:
                    state.mark_retry(meeting.source_key, error, "dispatching")
                    LOGGER.exception("Could not launch archive job for %s", meeting.source_key)
    return {"ready_count": ready_count, "dispatched_count": len(dispatched)}


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/")
def check_meetings():
    """The endpoint stays short; transcription happens in Cloud Run Jobs."""
    roots = [item.strip() for item in os.getenv("MEETING_SOURCE_FOLDER_IDS", "").split(",") if item.strip()]
    destination = os.getenv("ARCHIVE_DESTINATION_FOLDER_ID")
    job_name = os.getenv("ARCHIVE_JOB_NAME")
    start_time = os.getenv("ARCHIVE_START_TIME")
    if not roots or not destination or not job_name or not start_time:
        return jsonify({"error": "Missing archive checker configuration"}), 500
    not_before = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    if not_before.tzinfo is None:
        return jsonify({"error": "ARCHIVE_START_TIME must include a timezone"}), 500
    credentials, project_id = google.auth.default()
    del credentials
    state = ArchiveState(os.getenv("GOOGLE_CLOUD_PROJECT") or project_id)
    drive = build("drive", "v3", credentials=drive_credentials(), cache_discovery=False)
    result = check_once(
        roots, destination, job_name, state, drive,
        dispatch_limit=int(os.getenv("ARCHIVE_DISPATCH_LIMIT", "5")),
        not_before=not_before,
    )
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
