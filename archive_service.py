"""Cloud Run entry point for the separate Google Meet audio archive flow."""

import logging
import os

from flask import Flask, jsonify

from audio_archive import run_archive


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/")
def archive_meetings():
    """Cloud Scheduler invokes this once; Drive properties prevent duplicate Docs."""
    roots = [item.strip() for item in os.getenv("MEETING_SOURCE_FOLDER_IDS", "").split(",") if item.strip()]
    destination = os.getenv("ARCHIVE_DESTINATION_FOLDER_ID")
    key = os.getenv("GOOGLE_AI_API_KEY")
    if not roots or not destination or not key:
        return jsonify({"error": "Missing archive configuration"}), 500
    created = []
    max_meetings = int(os.getenv("ARCHIVE_MAX_MEETINGS_PER_ROOT", "3"))
    for root in roots:
        created.extend(run_archive(root, destination, key, max_meetings=max_meetings))
    return jsonify({"archived_doc_ids": created, "count": len(created)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
