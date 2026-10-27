"""Cloud Run Job entry point for long Google Meet audio archives."""

import logging
import os

from audio_archive import run_archive


def main():
    """Run one bounded batch; Cloud Scheduler can start the job later."""
    roots = [item.strip() for item in os.getenv("MEETING_SOURCE_FOLDER_IDS", "").split(",") if item.strip()]
    destination = os.getenv("ARCHIVE_DESTINATION_FOLDER_ID")
    key = os.getenv("GOOGLE_AI_API_KEY")
    if not roots or not destination or not key:
        raise ValueError("Missing archive configuration")
    max_meetings = int(os.getenv("ARCHIVE_MAX_MEETINGS_PER_ROOT", "3"))
    logging.basicConfig(level=logging.INFO)
    for root in roots:
        created = run_archive(root, destination, key, max_meetings=max_meetings)
        logging.info("Archived %s meetings from source folder %s", len(created), root)


if __name__ == "__main__":
    main()
