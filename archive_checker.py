"""Fast Cloud Run checker, intended for a 15-minute Cloud Scheduler cadence."""

import json
import logging
import os
import re
from datetime import datetime

import google.auth
from flask import Flask, jsonify
from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from analysis_state import AnalysisState
from archive_state import ArchiveState
from audio_archive import (drive_credentials, find_archived_recordings,
                           find_meetings_across_roots)
from meeting_auto_artifacts import (calendar_client, calendar_ids,
                                    enable_upcoming_artifacts, meet_settings_client)


LOGGER = logging.getLogger(__name__)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def launch_job(job_name, source_key, session=None, env_name="ARCHIVE_STATE_ID"):
    """Start one Cloud Run Job execution with a single state key override."""
    if not re.fullmatch(r"projects/[^/]+/locations/[^/]+/jobs/[^/]+", job_name):
        raise ValueError("Job name must be a full Cloud Run Job resource name")
    if session is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
    response = session.post(
        f"https://run.googleapis.com/v2/{job_name}:run",
        json={"overrides": {"containerOverrides": [{"env": [
            {"name": env_name, "value": source_key}
        ]}]}},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("name", "")


def launch_analysis_job(job_name, source_key):
    return launch_job(job_name, source_key, env_name="ANALYSIS_STATE_ID")


def dispatch_analysis_jobs(state, job_name, dispatch_limit=5,
                           launch=launch_analysis_job):
    """Dispatch briefs independently from long audio archive work."""
    dispatched = []
    for source_key in state.candidates():
        if len(dispatched) >= dispatch_limit:
            break
        if not state.reserve_for_dispatch(source_key):
            continue
        try:
            execution = launch(job_name, source_key)
            state.mark_queued(source_key, execution)
            dispatched.append(source_key)
        except Exception as error:
            state.mark_retry(source_key, error, "dispatching")
            LOGGER.exception("Could not launch analysis job for %s", source_key)
    return dispatched


def check_once(roots, destination_folder_id, job_name, state, drive, dispatch_limit=5,
               launch=launch_job, not_before=None):
    """Pair artifacts inside each owner's root group before dispatching."""
    archived = find_archived_recordings(drive, destination_folder_id)
    dispatched = []
    ready_count = 0
    seen_docs = set()
    seen_recordings = set()
    for group in roots:
        root_ids = [group] if isinstance(group, str) else list(group)
        for meeting in find_meetings_across_roots(drive, root_ids, not_before=not_before):
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
            if not state.reserve_for_dispatch(meeting, root_ids[0], root_ids=root_ids):
                continue
            try:
                execution = launch(job_name, meeting.source_key)
                state.mark_queued(meeting.source_key, execution)
                dispatched.append(meeting.source_key)
            except Exception as error:
                state.mark_retry(meeting.source_key, error, "dispatching")
                LOGGER.exception("Could not launch archive job for %s", meeting.source_key)
    return {"ready_count": ready_count, "dispatched_count": len(dispatched)}


def source_groups(flat_roots, groups_json=None):
    """Explicit groups prevent accidentally pairing meetings from different owners."""
    groups = json.loads(groups_json) if groups_json else [[root] for root in flat_roots]
    if (not isinstance(groups, list) or not groups
            or any(not isinstance(group, list) or not group or
                   any(not isinstance(root, str) or not root for root in group)
                   for group in groups)):
        raise ValueError("MEETING_SOURCE_FOLDER_GROUPS_JSON must be nonempty root ID arrays")
    ids = [root for group in groups for root in group]
    if len(ids) != len(set(ids)):
        raise ValueError("A Google Meet root may appear in only one owner group")
    return groups


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/auto-artifacts")
def auto_artifacts_upcoming():
    """Run premeeting Meet settings separately from the archive scanner."""
    try:
        owners = calendar_ids(os.getenv("MEETING_CALENDAR_IDS", ""))
    except ValueError as error:
        return jsonify({"error": str(error)}), 500
    service_account_email = os.getenv("MEETING_MEET_SERVICE_ACCOUNT_EMAIL")
    if not service_account_email:
        return jsonify({"error": "Missing MEETING_MEET_SERVICE_ACCOUNT_EMAIL"}), 500
    calendar = calendar_client()
    result = enable_upcoming_artifacts(
        calendar,
        lambda owner: meet_settings_client(service_account_email, owner),
        owners,
    )
    return jsonify(result), 502 if result["errors"] else 200


@app.post("/")
def check_meetings():
    """The endpoint stays short; transcription happens in Cloud Run Jobs."""
    flat_roots = [item.strip() for item in os.getenv("MEETING_SOURCE_FOLDER_IDS", "").split(",") if item.strip()]
    roots = source_groups(flat_roots, os.getenv("MEETING_SOURCE_FOLDER_GROUPS_JSON"))
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
    analysis_job_name = os.getenv("ANALYSIS_JOB_NAME")
    if analysis_job_name:
        analysis_state = AnalysisState(os.getenv("GOOGLE_CLOUD_PROJECT") or project_id)
        result["analysis_dispatched_count"] = len(dispatch_analysis_jobs(
            analysis_state, analysis_job_name,
            dispatch_limit=int(os.getenv("ANALYSIS_DISPATCH_LIMIT", "5")),
        ))
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
