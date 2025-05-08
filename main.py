import os
import json
import logging
from datetime import datetime
from drive_api import DriveAPI
from gemini_api import GeminiAPI
from chat_api import ChatAPI
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from io import BytesIO

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# Add loggers for key components
gemini_logger = logging.getLogger('gemini')
gemini_logger.setLevel(logging.INFO)
drive_logger = logging.getLogger('drive')
drive_logger.setLevel(logging.INFO)

app = Flask(__name__)

def setup_apis():
    """Initialize API clients."""
    try:
        load_dotenv()
        
        # Get environment variables (now plain text from secrets)
        folder_mapping = json.loads(os.getenv('FOLDER_MAPPING', '{}'))
        users_to_process = os.getenv('USERS_TO_PROCESS', '').strip().split(',')
        
        # Get and validate Gemini API key
        api_key = os.getenv('GOOGLE_AI_API_KEY')
        if not api_key:
            raise ValueError("GOOGLE_AI_API_KEY environment variable is not set")
        
        drive_api = DriveAPI(folder_mapping=folder_mapping, users_to_process=users_to_process)
        gemini_api = GeminiAPI(api_key)
        chat_api = ChatAPI(os.getenv('CHAT_WEBHOOK_URL'))
        validation_chat_api = ChatAPI(os.getenv('VALIDATION_CHAT_WEBHOOK_URL'))
        
        return drive_api, gemini_api, chat_api, validation_chat_api
    except ValueError as ve:
        logging.error(f"Configuration error: {str(ve)}")
        raise
    except Exception as e:
        logging.error(f"Failed to setup APIs: {str(e)}")
        raise

def process_meet_files(drive_api, gemini_api, chat_api, validation_chat_api, user_email, processed_titles):
    """Process new Meet files for a user."""
    # No clearing of processed statuses
    try:
        logging.info(f"Processing files for user: {user_email} (using service account credentials)")
        new_files = drive_api.get_new_meet_files(user_email)
        if not new_files:
            logging.info(f"No new Meet files found for user {user_email}")
            return
        logging.info(f"Found {len(new_files)} files to process")
        today = datetime.now().date()
        batch_size = 10
        for i in range(0, len(new_files), batch_size):
            batch = new_files[i:i + batch_size]
            logging.info(f"Processing batch {i//batch_size + 1} of {(len(new_files) + batch_size - 1)//batch_size}")
            for file in batch:
                try:
                    file_metadata = drive_api.service.files().get(
                        fileId=file['id'],
                        fields='mimeType,name,createdTime',
                        supportsAllDrives=True
                    ).execute()
                    mime_type = file_metadata.get('mimeType')
                    file_name = file_metadata.get('name', file.get('name', 'unknown'))
                    created_time = datetime.fromisoformat(file_metadata.get('createdTime').replace('Z', '+00:00'))
                    if created_time.date() != today:
                        logging.info(f"Skipping file {file_name} as it wasn't created today")
                        continue
                    if file_name in processed_titles:
                        logging.info(f"Skipping duplicate file title in this run: {file_name}")
                        continue
                    processed_titles.add(file_name)
                    logging.info(f"Processing file: {file_name} (type: {mime_type})")
                    if mime_type == 'application/vnd.google-apps.document':
                        doc = drive_api.service.files().export(
                            fileId=file['id'],
                            mimeType='text/plain'
                        ).execute()
                        if not doc:
                            logging.warning(f"No content found in document: {file_name}")
                            continue
                        content = doc.decode('utf-8') if isinstance(doc, bytes) else doc
                        logging.info(f"Sending content to Gemini for file: {file_name}")
                        meeting_type = gemini_api.determine_meeting_type(content, document_name=file_name)
                        logging.info(f"Determined meeting type: {meeting_type} for file: {file_name}")
                        doc_summary = gemini_api.summarize_transcript(content)
                        logging.info(f"Gemini document summary for {file_name}: {repr(doc_summary)}")
                        if not doc_summary:
                            logging.warning(f"Gemini summary was empty for file: {file_name}. Skipping summary insertion.")
                            continue
                        summary_text = f"\n\n=== AI-Generated Summary ===\n{doc_summary}\n"
                        try:
                            document = drive_api.docs_service.documents().get(documentId=file['id']).execute()
                            logging.info(f"Successfully retrieved document structure for {file_name}")
                            content_items = document.get('body', {}).get('content', [])
                            end_index = None
                            for item in reversed(content_items):
                                if 'endIndex' in item and not item.get('sectionBreak'):
                                    end_index = item['endIndex']
                                    logging.debug(f"Found end index: {end_index} in content item: {item}")
                                    break
                            if end_index is None:
                                logging.warning("No valid end index found, defaulting to 1")
                                end_index = 1
                            requests = [
                                {
                                    'insertText': {
                                        'location': {
                                            'index': end_index - 1
                                        },
                                        'text': summary_text
                                    }
                                }
                            ]
                            drive_api.docs_service.documents().batchUpdate(
                                documentId=file['id'],
                                body={'requests': requests}
                            ).execute()
                            logging.info(f"Successfully added summary to document: {file_name}")
                        except Exception as update_error:
                            logging.error(f"Failed to update document with summary for {file_name}: {str(update_error)}. Ensure the service account has editor rights.")
                            continue
                        if meeting_type in drive_api.folder_mapping:
                            destination_folder = drive_api.folder_mapping[meeting_type]
                            logging.info(f"Copying file {file_name} to folder: {meeting_type}")
                            if drive_api.copy_file(file['id'], destination_folder):
                                logging.info(f"Successfully copied file {file_name}")
                                drive_api.mark_all_with_title_as_processed(file_name)
                                logging.info(f"Successfully marked all files with title {file_name} as processed")
                                if meeting_type == "Daily Team Meeting":
                                    meeting_time = None
                                    if " - " in file_name:
                                        try:
                                            time_part = file_name.split(" - ")[2].split(" ")[0]
                                            meeting_time = time_part
                                        except:
                                            pass
                                    logging.info(f"Generating chat summary for daily meeting: {file_name}")
                                    chat_summary = gemini_api.generate_chat_summary(content, meeting_time)
                                    logging.info(f"Gemini chat summary for {file_name}: {repr(chat_summary)}")
                                    if chat_summary:
                                        logging.info(f"Sending chat summary for {file_name} to Google Chat")
                                        chat_api.send_daily_meeting_summary([{'name': file_name, 'summary': chat_summary}])
                                    else:
                                        logging.error(f"Failed to generate chat summary for {file_name}")
                                elif meeting_type in ["User Research Meeting", "Product Development Meeting"]:
                                    meeting_time = None
                                    if " - " in file_name:
                                        try:
                                            time_part = file_name.split(" - ")[2].split(" ")[0]
                                            meeting_time = time_part
                                        except:
                                            pass
                                    logging.info(f"Generating user validation summary for meeting: {file_name}")
                                    validation_summary = gemini_api.generate_user_validation_summary(content, meeting_time)
                                    logging.info(f"Gemini user validation summary for {file_name}: {repr(validation_summary)}")
                                    if validation_summary:
                                        logging.info(f"Sending user validation summary for {file_name} to Google Chat")
                                        validation_chat_api.send_daily_meeting_summary([{'name': file_name, 'summary': validation_summary}])
                                    else:
                                        logging.error(f"Failed to generate user validation summary for {file_name}")
                            else:
                                logging.error(f"Failed to copy file {file_name}")
                        else:
                            logging.warning(f"Unknown meeting type '{meeting_type}' for file: {file_name}")
                    else:
                        logging.info(f"Skipping non-Google Doc file: {file_name}")
                except Exception as e:
                    logging.error(f"Error processing file {file.get('name', 'unknown')}: {str(e)}")
                    continue
            logging.info(f"Completed processing batch {i//batch_size + 1}")
    except Exception as e:
        logging.error(f"Failed to process Meet files: {str(e)}")
        raise

@app.route('/', methods=['GET', 'POST'])
def handle_request():
    """Handle incoming requests."""
    try:
        logging.info(f"Starting request handling - Method: {request.method}")
        drive_api, gemini_api, chat_api, validation_chat_api = setup_apis()
        logging.info("APIs setup completed")
        users = os.getenv('USERS_TO_PROCESS', '').strip().split(',')
        users = [user.strip() for user in users if user.strip()]
        logging.info(f"Users to process: {users}")
        results = []
        processed_titles = set()  # Track processed file titles globally for this run
        for user_email in users:
            try:
                logging.info(f"\n=== Starting processing for user: {user_email} ===")
                process_meet_files(drive_api, gemini_api, chat_api, validation_chat_api, user_email, processed_titles)
                results.append({"user": user_email, "status": "success"})
                logging.info(f"=== Completed processing for user: {user_email} ===\n")
            except Exception as e:
                error_msg = f"Failed to process user {user_email}: {str(e)}"
                logging.error(error_msg)
                results.append({"user": user_email, "status": "error", "error": str(e)})
                continue
        return jsonify({
            "status": "completed",
            "results": results,
            "summary": f"Processed {len(users)} users"
        }), 200
    except Exception as e:
        error_msg = f"Fatal error in request handling: {str(e)}"
        logging.error(error_msg)
        return jsonify({"status": "error", "message": error_msg}), 500

def escape_query_string(text):
    return text.replace("\\", "\\\\").replace("'", "\\'")

if __name__ == "__main__":
    # Get port from environment variable or default to 8080
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port) 