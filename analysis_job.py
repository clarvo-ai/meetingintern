"""One Cloud Run Job creates a meeting brief and posts its Chat update."""

import logging
import os

import google.auth
from googleapiclient.discovery import build

from analysis_state import AnalysisState
from audio_archive import drive_credentials
from meeting_evidence import (archived_speech_body, calendar_client,
                              calendar_context, parse_meet_documents,
                              parse_team_roster, resolve_speaker_identities,
                              source_evidence)
from meeting_analysis import (analyze_transcript, create_or_find_brief,
                              format_chat_message, post_chat_message,
                              ChatDeliveryRejected, ChatDeliveryUncertain)


LOGGER = logging.getLogger(__name__)


def work_once(source_key, state, drive, docs, api_key, webhook_url, space_id,
              model="gemini-3.5-flash-lite", roster=None, calendar_ids=(), calendar=None):
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
            hints = ""
            evidence = {"speakers": [], "speaker_turns": {}, "attendees_in_meet_doc": [],
                        "source_speech_words": 0, "source_doc_created_at": None,
                        "confirmed_meet_participants": [],
                        "meet_participant_record_status": "unavailable"}
            if source_ids or data.get("source_recording_id"):
                try:
                    evidence.update(source_evidence(drive, source_ids,
                                                    data.get("source_recording_id")))
                    hints = ", ".join(dict.fromkeys([
                        *(evidence.get("speakers") or []),
                        *(evidence.get("attendees_in_meet_doc") or []),
                    ]))
                except Exception:
                    LOGGER.warning("Could not read Meet identity evidence for %s", source_key)
            evidence["archived_speech_words"] = speech["source_speech_words"]
            evidence.update(calendar_context(calendar, calendar_ids, evidence))
            evidence.update(resolve_speaker_identities(evidence, roster or {}))
            event = evidence.get("calendar_event") or {}
            calendar_hints = ", ".join(
                f"{person['name']} <{person['email']}>" if person.get("name") else person["email"]
                for person in event.get("invitees", [])
            )
            # A filename with names does not establish which person represented which side.
            analysis_title = title if hints else "Meeting recording"
            payload = analyze_transcript(api_key, analysis_title, transcript, hints, model,
                                         evidence=evidence, roster=roster or {},
                                         calendar_hints=calendar_hints)
        brief_doc_id = data.get("analysis_brief_doc_id") or create_or_find_brief(
            drive, docs, item["parents"][0], source_key,
            transcript_doc_id, title, payload,
        )
        chat_text = data.get("analysis_chat_text") or format_chat_message(
            title, transcript_doc_id, brief_doc_id, payload,
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
    work_once(source_key, state, drive, docs, api_key, webhook_url, space_id,
              os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"), roster,
              calendar_ids, calendar_client(calendar_ids))


if __name__ == "__main__":
    main()
