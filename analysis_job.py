"""One Cloud Run Job creates a meeting brief and posts its Chat update."""

import logging
import os

import google.auth
from googleapiclient.discovery import build

from analysis_state import AnalysisState
from audio_archive import drive_credentials, owner_drive_credentials
from company_reference import parse_company_references
from meeting_evidence import (archived_speech_body, calendar_client,
                              calendar_context, meet_client_for_user,
                              meet_participant_context, parse_meet_documents,
                              parse_team_roster, resolve_speaker_identities,
                              source_evidence)
from meeting_analysis import (create_or_find_brief, format_chat_message, post_chat_message,
                              ChatDeliveryRejected, ChatDeliveryUncertain)
from meeting_report_v2 import generate_report, format_report_brief, format_report_chat


LOGGER = logging.getLogger(__name__)


def work_once(source_key, state, drive, docs, api_key, webhook_url, space_id,
              model="gemini-3.5-flash-lite", roster=None, calendar_ids=(), calendar=None,
              meet_factory=None, company_references=None, source_drive_factory=None):
    """Resume from a saved brief if a previous execution stopped before Chat."""
    data = state.claim_for_work(source_key)
    if not data:
        LOGGER.info("Analysis %s is already claimed or posted", source_key)
        return None
    try:
        transcript_doc_id = data["output_doc_id"]
        item = drive.files().get(
            fileId=transcript_doc_id,
            fields="id,name,parents,mimeType",
            supportsAllDrives=True,
        ).execute()
        if not item.get("parents"):
            raise RuntimeError("Archived transcript has no destination folder")
        title = item["name"].removesuffix(" - English Transcript")
        payload = data.get("analysis_payload")
        if not payload:
            raw = drive.files().export(fileId=transcript_doc_id, mimeType="text/plain").execute()
            archived = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
            transcript = archived_speech_body(archived)
            speech = parse_meet_documents([("English Transcript", transcript)])
            if speech["source_speech_words"] < 3:
                state.mark_invalid_source(source_key, "Archived transcript contains no spoken words")
                LOGGER.warning("Skipping analysis %s: archived transcript has no speech", source_key)
                return None
            notes_id = data.get("source_notes_id")
            source_ids = list(dict.fromkeys([notes_id, *(data.get("source_context_doc_ids") or [])]))
            source_ids = [doc_id for doc_id in source_ids if doc_id]
            evidence = {"speakers": [], "speaker_turns": {}, "attendees_in_meet_doc": [],
                        "source_speech_words": 0, "source_doc_created_at": None,
                        "confirmed_meet_participants": [],
                        "meet_participant_record_status": "unavailable"}
            if source_ids or data.get("source_recording_id"):
                try:
                    owner = data.get("source_owner_email")
                    recording_owner = data.get("recording_owner_email") or owner
                    allowed = {email.casefold() for email in calendar_ids}
                    source_drive = (source_drive_factory(owner)
                                    if source_drive_factory and owner in allowed else drive)
                    recording_drive = (source_drive_factory(recording_owner)
                                       if source_drive_factory and recording_owner in allowed
                                       else drive)
                    evidence.update(source_evidence(
                        source_drive, source_ids, data.get("source_recording_id"),
                        recording_drive=recording_drive))
                except Exception:
                    LOGGER.warning("Could not read Meet identity evidence for %s", source_key)
            evidence["archived_speech_words"] = speech["source_speech_words"]
            evidence.update(calendar_context(calendar, calendar_ids, evidence))
            evidence.update(meet_participant_context(meet_factory, evidence, calendar_ids))
            evidence.update(resolve_speaker_identities(evidence, roster or {}))
            payload = generate_report(api_key, transcript, evidence, roster or {}, model,
                                      company_references or [])
        report_v2 = "headline" in payload
        brief_doc_id = data.get("analysis_brief_doc_id") or create_or_find_brief(
            drive, docs, item["parents"][0], source_key,
            transcript_doc_id, title, payload,
            formatter=(lambda _title, doc_id, report: format_report_brief(
                report, doc_id, roster or {})) if report_v2 else None,
            schema_version="2" if report_v2 else "1",
        )
        chat_text = data.get("analysis_chat_text") or (
            format_report_chat(payload, transcript_doc_id, brief_doc_id, roster or {})
            if report_v2 else format_chat_message(title, transcript_doc_id, brief_doc_id, payload)
        )
        state.save_brief(source_key, brief_doc_id, payload, chat_text)
        state.mark_sending(source_key)
        try:
            message_name = post_chat_message(webhook_url, space_id, source_key, chat_text)
        except ChatDeliveryRejected as error:
            state.mark_retry(source_key, error, "sending")
            raise
        except ChatDeliveryUncertain as error:
            state.mark_delivery_unknown(source_key, error)
            raise
        state.mark_posted(source_key, message_name)
        LOGGER.info("Analyzed %s into brief %s and Chat %s", source_key,
                    brief_doc_id, message_name)
        return brief_doc_id
    except Exception as error:
        state.mark_retry(source_key, error, "processing")
        raise


def main():
    logging.basicConfig(level=logging.INFO)
    source_key = os.getenv("ANALYSIS_STATE_ID")
    api_key = os.getenv("GOOGLE_AI_API_KEY")
    webhook_url = os.getenv("MEETING_ARCHIVE_CHAT_WEBHOOK")
    space_id = os.getenv("MEETING_CHAT_SPACE_ID")
    if not source_key or not api_key or not webhook_url or not space_id:
        raise ValueError("Missing meeting analysis configuration")
    _, project_id = google.auth.default()
    state = AnalysisState(os.getenv("GOOGLE_CLOUD_PROJECT") or project_id)
    credentials = drive_credentials()
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    docs = build("docs", "v1", credentials=credentials, cache_discovery=False)
    roster = parse_team_roster(os.getenv("MEETING_TEAM_ROSTER_JSON"))
    calendar_ids = tuple(item.strip() for item in os.getenv("MEETING_CALENDAR_IDS", "").split(",")
                         if item.strip())
    service_account_email = os.getenv("MEETING_MEET_SERVICE_ACCOUNT_EMAIL")
    meet_factory = (lambda user: meet_client_for_user(service_account_email, user)) if service_account_email else None
    source_drives = {}
    def source_drive(owner):
        if owner not in source_drives:
            source_drives[owner] = build(
                "drive", "v3",
                credentials=owner_drive_credentials(service_account_email, owner),
                cache_discovery=False,
            )
        return source_drives[owner]
    company_references = parse_company_references(os.getenv("MEETING_COMPANY_REFERENCE_JSON"))
    work_once(source_key, state, drive, docs, api_key, webhook_url, space_id,
              os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"), roster,
              calendar_ids, calendar_client(calendar_ids), meet_factory,
              company_references, source_drive if service_account_email else None)


if __name__ == "__main__":
    main()
