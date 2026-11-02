"""Preview or clean existing archived transcripts in place without changing links."""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from archive_job import cleanup_hints
from archive_state import ArchiveState
from audio_archive import (AudioGemini, CHUNK_SECONDS, DOC_MIME, Meeting,
                           drive_credentials, extract_audio_chunks,
                           owner_drive_credentials, recording_proxy)
from company_reference import parse_company_references
from meeting_auto_artifacts import calendar_ids
from meeting_evidence import calendar_client, parse_team_roster
from transcript_cleanup import CLEANUP_VERSION, clean_transcript, parse_spelling_hints


LOGGER = logging.getLogger(__name__)
METHOD_HEADER = re.compile(r"(?m)^Method: ([^\r\n]*)\r?\n(?:\r?\n)+")


def _document_text(document):
    """Refuse complex Docs rather than delete formatting, tabs or embedded data."""
    tabs = document.get("tabs")
    tab_id = None
    if tabs:
        if len(tabs) != 1 or tabs[0].get("childTabs"):
            raise ValueError("Transcript has multiple tabs")
        tab_id = tabs[0].get("tabProperties", {}).get("tabId")
        body = tabs[0].get("documentTab", {}).get("body", {})
    else:
        body = document.get("body", {})
    content = body.get("content") or []
    text = []
    for element in content:
        if "sectionBreak" in element:
            continue
        paragraph = element.get("paragraph")
        if not paragraph or paragraph.get("bullet"):
            raise ValueError("Transcript has non-plain document content")
        style = paragraph.get("paragraphStyle") or {}
        if (style.get("namedStyleType", "NORMAL_TEXT") != "NORMAL_TEXT" or
                set(style) - {"namedStyleType", "direction"}):
            raise ValueError("Transcript has paragraph formatting that would be lost")
        for part in paragraph.get("elements", []):
            run = part.get("textRun")
            if not run or set(run) - {"content", "textStyle", "suggestedTextStyleChanges"}:
                raise ValueError("Transcript has non-text document content")
            if run.get("textStyle") or run.get("suggestedTextStyleChanges"):
                raise ValueError("Transcript has formatting that would be lost")
            text.append(run.get("content", ""))
    if not content or not text or not document.get("revisionId"):
        raise ValueError("Transcript body or revision is missing")
    end_index = content[-1].get("endIndex")
    if not isinstance(end_index, int) or end_index < 2:
        raise ValueError("Transcript has no safe replacement range")
    return "".join(text), end_index, tab_id


def _prepared_body(original, gemini, context, audio_chunks=()):
    """Keep provenance and rewrite only the speech below the existing method header."""
    marker = METHOD_HEADER.search(original)
    if not marker:
        raise ValueError("Document is missing its archive provenance header")
    if "AI-cleaned" in marker.group(1):
        return None
    speech = original[marker.end():].strip()
    cleaned = clean_transcript(speech, gemini, context, audio_chunks, CHUNK_SECONDS)
    return original[:marker.start()] + f"Method: {marker.group(1)}; AI-cleaned\n\n" + cleaned


def _replace_body(docs, document_id, revision_id, end_index, tab_id, text):
    """Apply only to the revision read above, retaining the existing document ID."""
    location = {"index": 1}
    target_range = {"startIndex": 1, "endIndex": end_index - 1}
    if tab_id:
        location["tabId"] = tab_id
        target_range["tabId"] = tab_id
    docs.documents().batchUpdate(documentId=document_id, body={
        "requests": [{"deleteContentRange": {"range": target_range}},
                     {"insertText": {"location": location, "text": text.rstrip("\n")}}],
        "writeControl": {"requiredRevisionId": revision_id},
    }).execute()


def _preview_paths(directory, key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise ValueError("Unsafe archive state identifier")
    root = Path(directory)
    return root / f"{key}.txt", root / f"{key}.json"


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_preview(directory, key, doc_id, revision_id, original, replacement):
    """Save the reviewed text together with the precise source revision."""
    text_path, manifest_path = _preview_paths(directory, key)
    text_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = {"source_key": key, "document_id": doc_id, "revision_id": revision_id,
                "cleanup_version": CLEANUP_VERSION, "original_sha256": _digest(original),
                "cleaned_sha256": _digest(replacement)}
    written = []
    try:
        for path, content in ((text_path, replacement),
                              (manifest_path, json.dumps(manifest, ensure_ascii=False))):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            written.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def _read_preview(directory, key, doc_id, revision_id, original):
    """Refuse stale, mismatched or edited previews before touching Drive."""
    text_path, manifest_path = _preview_paths(directory, key)
    with text_path.open(encoding="utf-8") as source:
        replacement = source.read()
    with manifest_path.open(encoding="utf-8") as source:
        manifest = json.load(source)
    if (manifest.get("source_key") != key or manifest.get("document_id") != doc_id or
            manifest.get("revision_id") != revision_id or
            manifest.get("cleanup_version") != CLEANUP_VERSION or
            manifest.get("original_sha256") != _digest(original) or
            manifest.get("cleaned_sha256") != _digest(replacement)):
        raise ValueError("Preview is stale or has been modified; generate a new preview")
    return replacement


def backfill_one(key, data, drive, docs, gemini, destination, service_account,
                 owners, calendar, roster, references, apply_from=None, preview_dir=None,
                 spelling_hints=None):
    """Clean one archive record; default to a read-only preview."""
    doc_id = data.get("output_doc_id")
    if data.get("status") != "done" or not doc_id:
        return "not archived"
    metadata = drive.files().get(
        fileId=doc_id, fields="id,name,mimeType,parents,properties", supportsAllDrives=True,
    ).execute()
    properties = metadata.get("properties") or {}
    if (metadata.get("mimeType") != DOC_MIME or destination not in metadata.get("parents", [])
            or not properties.get("source_key")):
        raise ValueError("Document is not an archive transcript in the configured destination")
    if properties.get("transcript_cleanup_version") == CLEANUP_VERSION:
        return "already cleaned"
    document = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    original, end_index, tab_id = _document_text(document)
    if not METHOD_HEADER.search(original):
        raise ValueError("Document is missing its archive provenance header")
    if "AI-cleaned" in METHOD_HEADER.search(original).group(1):
        if apply_from:
            drive.files().update(fileId=doc_id, body={"properties": {
                **properties, "transcript_cleanup_version": CLEANUP_VERSION}},
                supportsAllDrives=True).execute()
        return "already cleaned"

    if apply_from:
        replacement = _read_preview(apply_from, key, doc_id, document["revisionId"], original)
        _replace_body(docs, doc_id, document["revisionId"], end_index, tab_id, replacement)
        updated = {**properties, "transcript_cleanup_version": CLEANUP_VERSION}
        drive.files().update(fileId=doc_id, body={"properties": updated}, supportsAllDrives=True).execute()
        return "cleaned"

    owner = data.get("source_owner_email")
    recording_owner = data.get("recording_owner_email") or owner
    source_credentials = (owner_drive_credentials(service_account, owner)
                          if service_account and owner else None)
    recording_credentials = (owner_drive_credentials(service_account, recording_owner)
                             if service_account and recording_owner else None)
    source_drive = build("drive", "v3", credentials=source_credentials, cache_discovery=False) if source_credentials else drive
    recording_drive = build("drive", "v3", credentials=recording_credentials, cache_discovery=False) if recording_credentials else drive
    notes = data.get("source_notes_id")
    recording = data.get("source_recording_id")
    meeting = Meeting(source_key=key, folder_id=data.get("meeting_folder_id") or "",
                      folder_name=metadata.get("name", ""), recording_id=recording,
                      recording_name=None, duration_seconds=None, notes_id=notes,
                      notes_name="", context_doc_ids=tuple(data.get("source_context_doc_ids") or ([notes] if notes else [])))
    hints = cleanup_hints(meeting, source_drive, recording_drive, calendar, owners, roster,
                          references, spelling_hints)
    audio_readable = bool(recording)
    if recording:
        try:
            recording_drive.files().get(fileId=recording, fields="id", supportsAllDrives=True).execute()
        except HttpError as error:
            if error.resp.status not in (403, 404):
                raise
            audio_readable = False
            LOGGER.warning("Source recording is unavailable for transcript %s", doc_id)
    with tempfile.TemporaryDirectory(prefix="transcript-backfill-") as temp:
        if audio_readable:
            media_credentials = recording_credentials or drive_credentials()
            with recording_proxy(media_credentials, recording) as url:
                chunks = extract_audio_chunks(url, temp)
            replacement = _prepared_body(original, gemini, hints, chunks)
        else:
            replacement = _prepared_body(original, gemini, hints)
    if replacement is None:
        return "already cleaned"
    if preview_dir:
        _write_preview(preview_dir, key, doc_id, document["revisionId"], original, replacement)
    return f"preview ready ({len(original)} → {len(replacement)} characters)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--state-id", help="One archive state document ID")
    choice.add_argument("--all", action="store_true", help="All completed archive records")
    parser.add_argument("--apply-from", help="Apply previously reviewed previews from a local directory")
    parser.add_argument("--preview-dir", help="Write cleaned previews to a local directory")
    args = parser.parse_args()
    if args.apply_from and args.preview_dir:
        parser.error("--preview-dir cannot be combined with --apply-from")
    logging.basicConfig(level=logging.INFO)
    api_key = os.getenv("GOOGLE_AI_API_KEY")
    destination = os.getenv("ARCHIVE_DESTINATION_FOLDER_ID")
    if not destination or (not api_key and not args.apply_from):
        raise ValueError("ARCHIVE_DESTINATION_FOLDER_ID and a preview-time GOOGLE_AI_API_KEY are required")
    _, project = google.auth.default()
    state = ArchiveState(os.getenv("GOOGLE_CLOUD_PROJECT") or project)
    credentials = drive_credentials()
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    service_account = os.getenv("MEETING_MEET_SERVICE_ACCOUNT_EMAIL")
    owners = calendar_ids(os.getenv("MEETING_CALENDAR_IDS", ""), allow_empty=True)
    roster = parse_team_roster(os.getenv("MEETING_TEAM_ROSTER_JSON"))
    references = parse_company_references(os.getenv("MEETING_COMPANY_REFERENCE_JSON"))
    spelling_hints = parse_spelling_hints(os.getenv("MEETING_TRANSCRIPT_TERMS_JSON"))
    calendar = calendar_client(owners)
    model = None if args.apply_from else AudioGemini(api_key, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    snapshots = ([state.collection.document(args.state_id).get()] if args.state_id
                 else state.collection.stream())
    failures = 0
    seen_docs = set()
    for snapshot in snapshots:
        if not snapshot.exists:
            print(f"{snapshot.id}: state record not found")
            failures += 1
            continue
        try:
            data = snapshot.to_dict()
            doc_id = data.get("output_doc_id")
            if doc_id and doc_id in seen_docs:
                print(f"{snapshot.id}: duplicate archive document")
                continue
            result = backfill_one(snapshot.id, data, drive, docs, model,
                                  destination, service_account, owners, calendar, roster,
                                  references, apply_from=args.apply_from, preview_dir=args.preview_dir,
                                  spelling_hints=spelling_hints)
            if doc_id and result != "not archived":
                seen_docs.add(doc_id)
            print(f"{snapshot.id}: {result}")
        except Exception as error:
            failures += 1
            LOGGER.error("Could not clean transcript %s: %s", snapshot.id, type(error).__name__)
    if failures:
        raise SystemExit(f"{failures} transcript(s) could not be processed")


if __name__ == "__main__":
    main()
