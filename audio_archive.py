"""Archive Google Meet transcripts after their recordings become available.

This is intentionally separate from the legacy summary and Chat pipeline.  It
reads personal Meet folders, writes a new Doc to a shared drive, and never edits
the source files.  The same flow runs locally or as a Cloud Run Job.
"""

import argparse
from contextlib import contextmanager
import hashlib
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import google.auth
import requests
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential


LOGGER = logging.getLogger(__name__)
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
RECORDING_MIME = "video/mp4"
CHUNK_SECONDS = 15 * 60
MAX_DURATION_SECONDS = 8 * 60 * 60
RECORDING_WAIT = timedelta(hours=1)
SETTLE_WAIT = timedelta(minutes=5)


def duration_exceeds_limit(seconds):
    """Exactly eight hours remains eligible; longer recordings do not."""
    return seconds is not None and seconds > MAX_DURATION_SECONDS


@dataclass(frozen=True)
class Meeting:
    source_key: str
    folder_id: str
    folder_name: str
    recording_id: str | None
    recording_name: str | None
    duration_seconds: float | None
    notes_id: str
    notes_name: str
    context_doc_ids: tuple[str, ...]


def drive_credentials(impersonate_service_account=None):
    """Use Cloud Run ADC, or a short-lived gcloud token for local verification."""
    if impersonate_service_account:
        source_token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token", "--quiet"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        source = Credentials(
            token=source_token,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=impersonate_service_account,
            target_scopes=["https://www.googleapis.com/auth/drive",
                           "https://www.googleapis.com/auth/documents"],
        )
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/documents"]
    )
    return credentials


def list_children(drive, folder_id):
    """Page through a folder; Google Drive does not recurse through parents."""
    files = []
    page_token = None
    while True:
        response = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,owners(emailAddress),shortcutDetails,videoMediaMetadata(durationMillis))",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return files


def _resolve_item(drive, item):
    if item["mimeType"] != SHORTCUT_MIME:
        return item
    target = item.get("shortcutDetails", {})
    if target.get("targetMimeType") not in (RECORDING_MIME, DOC_MIME):
        return None
    try:
        resolved = drive.files().get(
            fileId=target["targetId"],
            fields="id,name,mimeType,size,createdTime,modifiedTime,owners(emailAddress),videoMediaMetadata(durationMillis)",
            supportsAllDrives=True,
        ).execute()
        resolved["displayName"] = item["name"]
        return resolved
    except HttpError as error:
        if error.resp.status not in (403, 404):
            raise
        LOGGER.info("Cannot access shortcut target %s yet", target["targetId"])
        return None


def _meeting_time(name):
    match = re.search(r"(20\d\d)[/-](\d\d)[/-](\d\d) (\d\d):(\d\d)", name)
    return "".join(match.groups()) if match else None


def find_meetings(drive, folder, minimum_age_minutes=5, now=None, not_before=None):
    """Return ready meetings, preferring a settled recording over Doc fallback.

    A Doc without a recording becomes ready one hour after its last update.  A
    visible but unfinished recording blocks that fallback until it settles.
    """
    now = now or datetime.now(timezone.utc)
    settle_wait = timedelta(minutes=minimum_age_minutes)
    children = [_resolve_item(drive, item) for item in list_children(drive, folder["id"])]
    docs = [item for item in children if item and item["mimeType"] == DOC_MIME]
    recordings = [item for item in children if item and item["mimeType"] == RECORDING_MIME]
    groups = {}
    for doc in docs:
        key = _meeting_time(doc.get("displayName", doc["name"]))
        if key is None and len(docs) > 2:
            LOGGER.info("Skipping undated Doc %s in a recurring folder", doc["id"])
            continue
        groups.setdefault(key or "single", []).append(doc)
    meetings = []
    for key, candidates in groups.items():
        candidates.sort(
            key=lambda item: ("Transcript" in item["name"],
                              "Notes by Gemini" in item["name"]),
            reverse=True,
        )
        notes = candidates[0]
        if not_before and all(
            datetime.fromisoformat(item["createdTime"].replace("Z", "+00:00")) < not_before
            for item in candidates
        ):
            continue
        doc_modified = max(
            datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))
            for item in candidates
        )
        if now - doc_modified < settle_wait:
            continue
        matched = [item for item in recordings if
                   _meeting_time(item.get("displayName", item["name"])) == key]
        if not matched and len(groups) == len(recordings) == 1:
            matched = recordings
        matched.sort(key=lambda item: item.get("createdTime", ""), reverse=True)
        recording = matched[0] if matched else None
        if recording:
            video_modified = datetime.fromisoformat(recording["modifiedTime"].replace("Z", "+00:00"))
            if now - video_modified < settle_wait:
                continue
        elif now - doc_modified < RECORDING_WAIT:
            continue
        display_name = (recording.get("displayName", recording["name"]) if recording
                        else notes.get("displayName", notes["name"]))
        meeting_name = re.sub(r"\s+- (?:Recording|Transcript|Notes by Gemini)$", "", display_name)
        owners = sorted(person.get("emailAddress", "") for person in notes.get("owners", []))
        owner = next((email for email in owners if email), None)
        identity = f"{owner}|{key}" if owner and key != "single" else f"doc:{notes['id']}"
        source_key = hashlib.sha256(identity.encode()).hexdigest()
        meetings.append(Meeting(
            source_key=source_key,
            folder_id=folder["id"], folder_name=meeting_name,
            recording_id=recording["id"] if recording else None,
            recording_name=recording["name"] if recording else None,
            duration_seconds=(
                int(recording["videoMediaMetadata"]["durationMillis"]) / 1000
                if recording and recording.get("videoMediaMetadata", {}).get("durationMillis") else None
            ),
            notes_id=notes["id"], notes_name=notes["name"],
            context_doc_ids=tuple(item["id"] for item in candidates),
        ))
    return meetings


def find_archived_recordings(drive, destination_folder_id):
    """Accept old recording markers and new source keys for safe retries."""
    ids = set()
    page_token = None
    while True:
        response = drive.files().list(
            q=f"'{destination_folder_id}' in parents and trashed = false",
            fields="nextPageToken,files(id,properties)",
            pageSize=1000,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for item in response.get("files", []):
            properties = item.get("properties", {})
            ids.update(value for key, value in properties.items()
                       if key in ("source_key", "source_notes_id", "source_recording_id") and value)
        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


def existing_archive_doc_id(drive, destination_folder_id, meeting):
    """Recover output if a worker stopped after the Drive write completed."""
    page_token = None
    while True:
        response = drive.files().list(
            q=f"'{destination_folder_id}' in parents and trashed = false",
            fields="nextPageToken,files(id,properties)", pageSize=1000,
            pageToken=page_token, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for item in response.get("files", []):
            properties = item.get("properties", {})
            if (properties.get("source_key") == meeting.source_key or
                    properties.get("source_notes_id") == meeting.notes_id or
                    (meeting.recording_id and properties.get("source_recording_id") == meeting.recording_id)):
                return item["id"]
        page_token = response.get("nextPageToken")
        if not page_token:
            return None


def export_transcript(drive, meeting):
    raw = drive.files().export(fileId=meeting.notes_id, mimeType="text/plain").execute()
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    if "Transcript" in meeting.notes_name and "Notes by Gemini" not in meeting.notes_name:
        return text.strip()
    # Gemini Notes may include its editable transcript after the notes section.
    markers = list(re.finditer(r"(?im)^.*? - Transcript\s*$", text))
    if markers:
        return text[markers[-1].start():].strip()
    return ""


def person_context(drive, meeting):
    """Extract spelling hints, never speech content, from the Meet documents."""
    names = set()
    emails = set()
    for doc_id in meeting.context_doc_ids:
        raw = drive.files().export(fileId=doc_id, mimeType="text/plain").execute()
        content = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        emails.update(re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", content, re.I))
        transcript_start = list(re.finditer(r"(?im)^.*? - Transcript\s*$", content))
        transcript = content[transcript_start[-1].start():] if transcript_start else content
        for candidate in re.findall(r"(?m)^([^:\r\n]{2,70}):[ \t]", transcript):
            candidate = candidate.strip()
            if (re.search(r"[A-Za-zÀ-ž]", candidate) and
                    not re.search(r"\b(summary|details|agenda|action items)\b", candidate, re.I)):
                names.add(candidate)
    owner = drive.files().get(
        fileId=meeting.notes_id, fields="owners(displayName,emailAddress)",
        supportsAllDrives=True,
    ).execute().get("owners", [])
    for person in owner:
        if person.get("emailAddress"):
            emails.add(person["emailAddress"])
    name_list = sorted(names)[:30]
    email_list = sorted(emails)[:30]
    return (
        f"Possible speaker spellings from the Meet transcript: {', '.join(name_list) or 'none'}. "
        f"Emails found in the Meet documents or organizer metadata: {', '.join(email_list) or 'none'}. "
        "These are hints, not proof that each person attended or spoke. Use a name only "
        "when the audio supports the identification; otherwise use neutral speaker labels. "
        "Do not copy speech wording from the Meet transcript."
    )


@contextmanager
def recording_proxy(credentials, recording_id):
    """Give ffmpeg a seekable local URL backed by Drive range requests.

    Google Meet MP4 files may need a seek to read their metadata.  The proxy
    keeps the OAuth token out of ffmpeg arguments and avoids storing video in
    Cloud Run's memory-backed filesystem.
    """
    provider_url = f"https://www.googleapis.com/drive/v3/files/{recording_id}"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._relay("GET")

        def do_HEAD(self):
            self._relay("HEAD")

        def _relay(self, method):
            if self.path != "/recording.mp4":
                self.send_error(404)
                return
            headers = {}
            credentials.before_request(GoogleAuthRequest(), method, provider_url, headers)
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            try:
                with requests.request(
                    method, provider_url,
                    params={"alt": "media", "supportsAllDrives": "true"},
                    headers=headers, stream=True, timeout=(15, 300),
                ) as upstream:
                    self.send_response(upstream.status_code)
                    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                        if upstream.headers.get(name):
                            self.send_header(name, upstream.headers[name])
                    self.end_headers()
                    if method == "GET":
                        for chunk in upstream.iter_content(1024 * 1024):
                            if chunk:
                                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # ffmpeg closes a range request when it has enough metadata.
                pass

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/recording.mp4"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def probe_duration_seconds(recording_url):
    """Use ffprobe when Drive has not populated videoMediaMetadata yet."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", recording_url],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def extract_audio_chunks(recording_url, output_dir):
    """Compress the audio track and split it so each answer stays manageable."""
    pattern = str(Path(output_dir) / "audio_%04d.mp3")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", recording_url, "-map", "0:a:0", "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
         "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
         "-reset_timestamps", "1", pattern],
        check=True, capture_output=True, text=True,
    )
    chunks = sorted(Path(output_dir).glob("audio_*.mp3"))
    if not chunks:
        raise RuntimeError("The recording has no readable audio track")
    return chunks


class AudioGemini:
    def __init__(self, api_key, model="gemini-3.5-flash-lite"):
        self.client = genai.Client(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
    def _ask(self, prompt, audio_file, max_output_tokens):
        response = self.client.models.generate_content(
            model=self.model,
            contents=[prompt, audio_file],
            config=types.GenerateContentConfig(
                temperature=0, max_output_tokens=max_output_tokens,
            ),
        )
        if not response.text or not response.text.strip():
            raise RuntimeError("Gemini returned no text for an audio chunk")
        candidate = response.candidates[0] if response.candidates else None
        if candidate and str(candidate.finish_reason).upper().endswith("MAX_TOKENS"):
            raise RuntimeError("Gemini transcript exceeded the output limit")
        return response.text.strip()

    def _with_uploaded_audio(self, path, prompt, max_output_tokens):
        uploaded = self.client.files.upload(file=str(path))
        try:
            # Larger audio uploads may remain PROCESSING briefly after upload.
            for _ in range(30):
                state = str(self.client.files.get(name=uploaded.name).state).upper()
                if state.endswith("ACTIVE") or state == "NONE":
                    break
                if state.endswith("FAILED"):
                    raise RuntimeError("Gemini failed to process an uploaded audio chunk")
                time.sleep(2)
            else:
                raise TimeoutError("Gemini audio upload did not become active")
            return self._ask(prompt, uploaded, max_output_tokens)
        finally:
            try:
                self.client.files.delete(name=uploaded.name)
            except Exception:
                LOGGER.warning("Could not delete temporary Gemini upload %s", uploaded.name)

    def detect_language(self, chunks):
        """Check every chunk so a non-English section is not missed."""
        answers = []
        for chunk in chunks:
            answer = self._with_uploaded_audio(
                chunk,
                "Identify speech in this meeting audio. Reply with exactly NO_SPEECH "
                "if there is no intelligible speech, ENGLISH if all substantive "
                "speech is English, or OTHER if any substantive speech is in "
                "another language or you are unsure.",
                32,
            )
            answers.append(answer.strip().upper())
            if answers[-1] not in ("ENGLISH", "NO_SPEECH"):
                return "other"
        if "OTHER" in answers or any(answer not in ("ENGLISH", "NO_SPEECH") for answer in answers):
            return "other"
        if "ENGLISH" in answers:
            return "english"
        return "no_speech"

    def translate_chunks(self, chunks, people=""):
        sections = []
        for index, chunk in enumerate(chunks):
            text = self._with_uploaded_audio(
                chunk,
                "Transcribe all intelligible speech in this meeting audio. Translate "
                "non-English speech into natural English. Preserve the full meaning, "
                "including decisions, numbers, names, and action items. Do not summarize, "
                "invent speech, or omit repetitions. Use speaker labels only when you can "
                "distinguish speakers reliably; otherwise use neutral labels. Return only "
                f"the English transcript in chronological order. {people}",
                16384,
            )
            start = index * CHUNK_SECONDS
            sections.append(f"[Chunk starting {start // 3600:02d}:{(start // 60) % 60:02d}:{start % 60:02d}]\n{text}")
        return "\n\n".join(sections)


def create_archive_doc(drive, docs, destination_folder_id, meeting, transcript, method):
    """Publish only after transcription succeeds; remove an incomplete draft."""
    title = f"{meeting.folder_name} - English Transcript"
    properties = {
        "source_key": meeting.source_key,
        "source_notes_id": meeting.notes_id,
        "transcription_method": method,
    }
    if meeting.recording_id:
        properties["source_recording_id"] = meeting.recording_id
    body = {
        "name": title,
        "mimeType": DOC_MIME,
        "parents": [destination_folder_id],
        "properties": properties,
    }
    created = drive.files().create(body=body, fields="id,webViewLink", supportsAllDrives=True).execute()
    content = (
        f"{title}\n\n"
        f"Source recording: "
        f"{f'https://drive.google.com/file/d/{meeting.recording_id}/view' if meeting.recording_id else 'not available after one hour'}\n"
        f"Source notes: https://docs.google.com/document/d/{meeting.notes_id}/edit\n"
        f"Method: {method}\n\n{transcript}\n"
    )
    try:
        docs.documents().batchUpdate(
            documentId=created["id"],
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute()
    except Exception:
        drive.files().delete(fileId=created["id"], supportsAllDrives=True).execute()
        raise
    return created["id"]


def process_meeting(drive, docs, credentials, gemini, destination_folder_id, meeting):
    """Write one shared transcript from audio or the one-hour Doc fallback."""
    if not meeting.recording_id:
        transcript = export_transcript(drive, meeting)
        method = "Google Meet transcript (recording unavailable after one hour)"
        if not transcript:
            raise RuntimeError(f"Meet document {meeting.notes_id} has no transcript yet")
    else:
        if duration_exceeds_limit(meeting.duration_seconds):
            return None
        with tempfile.TemporaryDirectory(prefix="meeting-audio-") as workdir:
            with recording_proxy(credentials, meeting.recording_id) as recording_url:
                duration = meeting.duration_seconds
                if duration is None:
                    duration = probe_duration_seconds(recording_url)
                if duration_exceeds_limit(duration):
                    return None
                chunks = extract_audio_chunks(recording_url, workdir)
            language = gemini.detect_language(chunks)
            if language == "no_speech":
                return None
            if language == "english":
                transcript = export_transcript(drive, meeting)
                method = "Google Meet transcript (English)"
                if not transcript:
                    transcript = gemini.translate_chunks(chunks)
                    method = "Gemini audio transcription (English fallback)"
            else:
                transcript = gemini.translate_chunks(chunks, person_context(drive, meeting))
                method = "Gemini audio transcription and English translation"
    if not transcript:
        raise RuntimeError(f"No transcript for Meet document {meeting.notes_id}")
    return create_archive_doc(drive, docs, destination_folder_id, meeting, transcript, method)


def run_archive(source_folder_id, destination_folder_id, api_key,
                meeting_folder_id=None, impersonate_service_account=None,
                max_meetings=10, minimum_age_minutes=5):
    """Process complete, not-yet-archived meetings and return their new Doc IDs."""
    credentials = drive_credentials(impersonate_service_account)
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    gemini = AudioGemini(api_key, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    if meeting_folder_id:
        folders = [drive.files().get(
            fileId=meeting_folder_id, fields="id,name,mimeType", supportsAllDrives=True
        ).execute()]
    else:
        folders = [item for item in list_children(drive, source_folder_id)
                   if item["mimeType"] == FOLDER_MIME and item["name"] != "Meet Recordings"]
        folders.sort(key=lambda item: item.get("createdTime", ""), reverse=True)
    archived = find_archived_recordings(drive, destination_folder_id)
    created = []
    for folder in folders:
        if len(created) >= max_meetings:
            break
        for meeting in find_meetings(drive, folder, minimum_age_minutes):
            if len(created) >= max_meetings:
                break
            if (meeting.source_key in archived or meeting.notes_id in archived or
                    meeting.recording_id in archived):
                continue
            doc_id = process_meeting(drive, docs, credentials, gemini, destination_folder_id, meeting)
            if doc_id:
                archived.add(meeting.source_key)
                created.append(doc_id)
                LOGGER.info("Archived Meet document %s as Doc %s", meeting.notes_id, doc_id)
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-folder-id", required=True)
    parser.add_argument("--destination-folder-id", required=True)
    parser.add_argument("--meeting-folder-id")
    parser.add_argument("--impersonate-service-account")
    parser.add_argument("--max-meetings", type=int, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        parser.error("GOOGLE_AI_API_KEY is required")
    created = run_archive(
        args.source_folder_id, args.destination_folder_id, key,
        meeting_folder_id=args.meeting_folder_id,
        impersonate_service_account=args.impersonate_service_account,
        max_meetings=args.max_meetings,
    )
    for doc_id in created:
        print(f"https://docs.google.com/document/d/{doc_id}/edit")


if __name__ == "__main__":
    main()
