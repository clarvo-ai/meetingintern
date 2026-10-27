"""Archive Google Meet transcripts after their recordings become available.

This is intentionally separate from the legacy summary and Chat pipeline.  It
reads personal Meet folders, writes a new Doc to a shared drive, and never edits
the source files.  The same entry point runs locally or behind Cloud Scheduler.
"""

import argparse
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import google.auth
from google.auth import impersonated_credentials
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from tenacity import retry, stop_after_attempt, wait_exponential


LOGGER = logging.getLogger(__name__)
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
RECORDING_MIME = "video/mp4"
CHUNK_SECONDS = 15 * 60
MAX_VIDEO_BYTES = int(os.getenv("ARCHIVE_MAX_VIDEO_MB", "500")) * 1024 * 1024


@dataclass(frozen=True)
class Meeting:
    folder_id: str
    folder_name: str
    recording_id: str
    recording_name: str
    recording_size: int
    notes_id: str
    notes_name: str


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
            fields="nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,shortcutDetails)",
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
            fields="id,name,mimeType,size,createdTime,modifiedTime",
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
    match = re.search(r"20\d\d/\d\d/\d\d \d\d:\d\d", name)
    return match.group(0) if match else None


def find_meetings(drive, folder, minimum_age_minutes=5):
    """Match files by Meet's timestamp, including recurring-folder shortcuts."""
    children = [_resolve_item(drive, item) for item in list_children(drive, folder["id"])]
    docs = [item for item in children if item and item["mimeType"] == DOC_MIME]
    recordings = [item for item in children if item and item["mimeType"] == RECORDING_MIME]
    recordings.sort(key=lambda item: item.get("createdTime", ""), reverse=True)
    meetings = []
    for recording in recordings:
        recording_time = _meeting_time(recording.get("displayName", recording["name"]))
        candidates = [doc for doc in docs if recording_time and
                      _meeting_time(doc.get("displayName", doc["name"])) == recording_time]
        if not candidates and len(recordings) == len(docs) == 1:
            candidates = docs
        if not candidates:
            LOGGER.info("No matching transcript for recording %s yet", recording["id"])
            continue
        candidates.sort(
            key=lambda item: ("Transcript" in item["name"],
                              "Notes by Gemini" in item["name"]),
            reverse=True,
        )
        notes = candidates[0]
        last_changed = max(
            datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))
            for item in (recording, notes)
        )
        if (datetime.now(timezone.utc) - last_changed).total_seconds() < minimum_age_minutes * 60:
            continue
        display_name = recording.get("displayName", recording["name"])
        meeting_name = re.sub(r"\s+- Recording$", "", display_name)
        meetings.append(Meeting(
            folder_id=folder["id"], folder_name=meeting_name,
            recording_id=recording["id"], recording_name=recording["name"],
            recording_size=int(recording.get("size", 0)),
            notes_id=notes["id"], notes_name=notes["name"],
        ))
    return meetings


def find_archived_recordings(drive, destination_folder_id):
    """Use the destination file property as the durable duplicate marker."""
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
            recording_id = item.get("properties", {}).get("source_recording_id")
            if recording_id:
                ids.add(recording_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


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


def download_recording(drive, recording_id, target):
    """Stream media to disk without holding a large MP4 in process memory."""
    request = drive.files().get_media(fileId=recording_id, supportsAllDrives=True)
    with open(target, "wb") as output:
        downloader = MediaIoBaseDownload(output, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk(num_retries=3)
            if output.tell() > MAX_VIDEO_BYTES:
                raise ValueError("Recording exceeds the configured download limit")


def extract_audio_chunks(recording, output_dir):
    """Compress the audio track and split it so each answer stays manageable."""
    pattern = str(Path(output_dir) / "audio_%04d.mp3")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(recording), "-map", "0:a:0", "-vn", "-ac", "1",
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
        """Classify sampled audio, treating silence as a separate outcome."""
        sample = [chunks[0]]
        if len(chunks) > 1:
            sample.append(chunks[len(chunks) // 2])
        if len(chunks) > 2:
            sample.append(chunks[-1])
        answers = []
        for chunk in dict.fromkeys(sample):
            answer = self._with_uploaded_audio(
                chunk,
                "Identify speech in this meeting audio. Reply with exactly NO_SPEECH "
                "if there is no intelligible speech, ENGLISH if all substantive "
                "speech is English, or OTHER if any substantive speech is in "
                "another language or you are unsure.",
                32,
            )
            answers.append(answer.strip().upper())
        if "OTHER" in answers or any(answer not in ("ENGLISH", "NO_SPEECH") for answer in answers):
            return "other"
        if "ENGLISH" in answers:
            return "english"
        return "no_speech"

    def translate_chunks(self, chunks):
        sections = []
        for index, chunk in enumerate(chunks):
            text = self._with_uploaded_audio(
                chunk,
                "Transcribe all intelligible speech in this meeting audio. Translate "
                "non-English speech into natural English. Preserve the full meaning, "
                "including decisions, numbers, names, and action items. Do not summarize, "
                "invent speech, or omit repetitions. Use speaker labels only when you can "
                "distinguish speakers reliably; otherwise use neutral labels. Return only "
                "the English transcript in chronological order.",
                16384,
            )
            start = index * CHUNK_SECONDS
            sections.append(f"[Chunk starting {start // 3600:02d}:{(start // 60) % 60:02d}:{start % 60:02d}]\n{text}")
        return "\n\n".join(sections)


def create_archive_doc(drive, docs, destination_folder_id, meeting, transcript, method):
    """Publish only after transcription succeeds; remove an incomplete draft."""
    title = f"{meeting.folder_name} - English Transcript"
    body = {
        "name": title,
        "mimeType": DOC_MIME,
        "parents": [destination_folder_id],
        "properties": {
            "source_recording_id": meeting.recording_id,
            "source_notes_id": meeting.notes_id,
            "transcription_method": method,
        },
    }
    created = drive.files().create(body=body, fields="id,webViewLink", supportsAllDrives=True).execute()
    content = (
        f"{title}\n\n"
        f"Source recording: https://drive.google.com/file/d/{meeting.recording_id}/view\n"
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
            if meeting.recording_id in archived:
                continue
            if not meeting.recording_size or meeting.recording_size > MAX_VIDEO_BYTES:
                LOGGER.warning("Skipping recording %s: size unavailable or exceeds %s MB",
                               meeting.recording_id, MAX_VIDEO_BYTES // (1024 * 1024))
                continue
            with tempfile.TemporaryDirectory(prefix="meeting-audio-") as workdir:
                recording_path = Path(workdir) / "recording.mp4"
                download_recording(drive, meeting.recording_id, recording_path)
                chunks = extract_audio_chunks(recording_path, workdir)
                recording_path.unlink()
                language = gemini.detect_language(chunks)
                if language == "no_speech":
                    LOGGER.info("Skipping recording %s: no intelligible speech", meeting.recording_id)
                    continue
                if language == "english":
                    transcript = export_transcript(drive, meeting)
                    method = "Google Meet transcript (English)"
                    if not transcript:
                        transcript = gemini.translate_chunks(chunks)
                        method = "Gemini audio transcription (English fallback)"
                else:
                    transcript = gemini.translate_chunks(chunks)
                    method = "Gemini audio transcription and English translation"
                if not transcript:
                    raise RuntimeError(f"No transcript for recording {meeting.recording_id}")
                doc_id = create_archive_doc(
                    drive, docs, destination_folder_id, meeting, transcript, method
                )
                archived.add(meeting.recording_id)
                created.append(doc_id)
                LOGGER.info("Archived recording %s as Doc %s", meeting.recording_id, doc_id)
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
