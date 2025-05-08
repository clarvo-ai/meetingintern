import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, HttpRequest
import logging
import os
import json
import requests
from io import BytesIO
from datetime import datetime, timedelta

class DriveAPI:
    def __init__(self, folder_mapping=None, users_to_process=None):
        """Initialize the Drive API client using Application Default Credentials."""
        self.SCOPES = [
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/documents'
        ]
        # Customize this property name if needed
        self.PROCESSED_PROPERTY = 'meeting_processed'
        try:
            credentials, project = google.auth.default(scopes=self.SCOPES)
            self.project_id = project
            self.credentials = credentials
            self.service = build('drive', 'v3', credentials=self.credentials, requestBuilder=HttpRequest)
            self.docs_service = build('docs', 'v1', credentials=self.credentials, requestBuilder=HttpRequest)
            self.folder_mapping = folder_mapping or {}
            self.users_to_process = users_to_process or []
            logging.info("DriveAPI initialized successfully with service account credentials.")
        except Exception as e:
            logging.error(f"Failed to initialize DriveAPI: {str(e)}")
            raise

    def get_new_meet_files(self, user_email):
        """Get all unprocessed Meet transcripts from all Meet Recordings folders."""
        try:
            logging.info("Starting search for unprocessed Google Docs files only (excluding video recordings)")
            folder_query = (
                "name = 'Meet Recordings' "
                "and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            )
            logging.info(f"Searching for Meet Recordings folders with query: {folder_query}")
            folder_response = self.service.files().list(
                q=folder_query,
                spaces='drive',
                fields='files(id, name, owners)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora='allDrives'
            ).execute()
            if not folder_response.get('files'):
                logging.error(f"No Meet Recordings folders found. Response: {folder_response}")
                return []
            folders = folder_response['files']
            logging.info(f"Found {len(folders)} Meet Recordings folders")
            for folder in folders:
                logging.info(f"Found folder '{folder['name']}' with ID: {folder['id']}")
                if 'owners' in folder:
                    owner_emails = [owner.get('emailAddress', 'unknown') for owner in folder['owners']]
                    logging.info(f"Folder owners: {', '.join(owner_emails)}")
            all_files = []
            for folder in folders:
                try:
                    try:
                        folder_check = self.service.files().get(
                            fileId=folder['id'],
                            fields='id,name',
                            supportsAllDrives=True
                        ).execute()
                        logging.info(f"Successfully accessed folder: {folder_check['name']}")
                    except Exception as e:
                        logging.warning(f"Cannot access folder {folder.get('name', 'unknown')} ({folder['id']}). This is expected for folders we don't have access to yet. Error: {str(e)}")
                        continue
                    files_query = (
                        f"'{folder['id']}' in parents "
                        "and mimeType = 'application/vnd.google-apps.document' "
                        "and trashed = false"
                    )
                    logging.info(f"Searching for Google Docs files only in folder {folder['id']}")
                    logging.info(f"Query: {files_query}")
                    batch_size = 100
                    page_token = None
                    files_in_folder = 0
                    while True:
                        try:
                            response = self.service.files().list(
                                q=files_query,
                                spaces='drive',
                                fields='nextPageToken, files(id, name, modifiedTime)',
                                supportsAllDrives=True,
                                includeItemsFromAllDrives=True,
                                pageToken=page_token,
                                pageSize=batch_size
                            ).execute()
                            files = response.get('files', [])
                            if files:
                                unprocessed_files = []
                                for file in files:
                                    if not self.has_been_processed(file['id']):
                                        unprocessed_files.append(file)
                                        logging.info(f"- {file['name']} (modified: {file['modifiedTime']}) - Not yet processed")
                                    else:
                                        logging.debug(f"Skipping already processed file: {file['name']}")
                                files_in_folder += len(unprocessed_files)
                                all_files.extend(unprocessed_files)
                            page_token = response.get('nextPageToken')
                            if not page_token:
                                break
                            logging.info(f"Processing next batch of files from folder {folder['id']}")
                        except Exception as e:
                            logging.warning(f"Error listing files in folder {folder['id']}, skipping page: {str(e)}")
                            break
                    logging.info(f"Total files found in folder {folder['id']}: {files_in_folder}")
                except Exception as e:
                    logging.warning(f"Error processing folder {folder.get('name', 'unknown')} ({folder['id']}): {str(e)}")
                    continue
            logging.info(f"Total Google Docs files found across all accessible folders: {len(all_files)}")
            return all_files
        except Exception as e:
            logging.error(f"Failed to fetch Meet transcripts: {e}")
            return []

    def create_or_get_folder(self, folder_name):
        """Create a folder if it doesn't exist, or return existing folder ID."""
        try:
            # Check if folder exists in mapping
            if folder_name in self.folder_mapping:
                return self.folder_mapping[folder_name]
            
            # Search for existing folder
            query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'"
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=False
            ).execute()
            
            files = results.get('files', [])
            if files:
                return files[0]['id']
            
            # Create new folder
            folder_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            
            folder = self.service.files().create(
                body=folder_metadata,
                fields='id'
            ).execute()
            
            return folder['id']
            
        except Exception as e:
            logging.error(f"Failed to create/get folder {folder_name}: {str(e)}")
            raise

    def move_file(self, file_id, folder_id):
        """Move file to destination folder."""
        try:
            # Get file metadata
            file = self.service.files().get(
                fileId=file_id,
                fields='name, parents',
                supportsAllDrives=True
            ).execute()
            
            # Move file to new folder
            self.service.files().update(
                fileId=file_id,
                addParents=folder_id,
                removeParents=','.join(file.get('parents', [])),
                fields='id, parents',
                supportsAllDrives=True
            ).execute()
            
            logging.info(f"Successfully moved file {file['name']} to folder {folder_id}")
            return True
        except Exception as e:
            logging.error(f"Failed to move file {file_id}: {str(e)}")
            return False

    def verify_folder_access(self, folder_id):
        """Verify that we have access to the destination folder."""
        try:
            folder = self.service.files().get(
                fileId=folder_id,
                fields='id, name, driveId, capabilities',
                supportsAllDrives=True
            ).execute()
            
            logging.info(f"Successfully verified access to folder: {folder.get('name', 'unknown')} (ID: {folder_id})")
            logging.info(f"Folder is in drive: {folder.get('driveId', 'personal drive')}")
            logging.info(f"Folder capabilities: {folder.get('capabilities', {})}")
            return True
        except Exception as e:
            logging.error(f"Cannot access folder {folder_id}: {str(e)}")
            return False

    def copy_file(self, file_id, folder_id):
        """Copy file to destination folder."""
        try:
            # First verify we have access to the destination folder
            if not self.verify_folder_access(folder_id):
                logging.error(f"Cannot access destination folder {folder_id}")
                return False
            # Get file metadata including the driveId
            file = self.service.files().get(
                fileId=file_id,
                fields='name, driveId',
                supportsAllDrives=True
            ).execute()
            # Create a copy in the destination folder
            copied_file = self.service.files().copy(
                fileId=file_id,
                body={
                    'name': file['name'],
                    'parents': [folder_id]
                },
                supportsAllDrives=True
            ).execute()
            logging.info(f"Successfully copied file {file['name']} to shared drive folder {folder_id}")
            return True
        except Exception as e:
            logging.error(f"Failed to copy file {file_id} to shared drive: {str(e)}")
            return False

    def has_been_processed(self, file_id):
        """Check if a file has already been processed by looking for our custom property."""
        try:
            file = self.service.files().get(
                fileId=file_id,
                fields='properties',
                supportsAllDrives=True
            ).execute()
            processed = file.get('properties', {}).get(self.PROCESSED_PROPERTY) == 'true'
            logging.info(f"Checked processed status for file {file_id}: {processed}")
            return processed
        except Exception as e:
            logging.error(f"Failed to check if file {file_id} was processed: {str(e)}")
            return False

    def mark_as_processed(self, file_id):
        """Mark a file as processed by setting a custom property."""
        try:
            self.service.files().update(
                fileId=file_id,
                body={
                    'properties': {
                        self.PROCESSED_PROPERTY: 'true'
                    }
                },
                supportsAllDrives=True
            ).execute()
            logging.info(f"Marked file {file_id} as processed ({self.PROCESSED_PROPERTY}=true)")
            return True
        except Exception as e:
            logging.error(f"Failed to mark file {file_id} as processed: {str(e)}")
            return False

    def verify_document_access(self, doc_id):
        try:
            doc = self.docs_service.documents().get(
                documentId=doc_id,
                fields='title,documentId'
            ).execute()
            logging.info(f"Successfully accessed document: {doc.get('title', 'unknown')}")
            try:
                # Insert a single space to check write access (API does not allow empty string)
                self.docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={
                        'requests': [
                            {
                                'insertText': {
                                    'location': {
                                        'index': 1
                                    },
                                    'text': ' '  # Insert a single space to satisfy the API
                                }
                            }
                        ]
                    }
                ).execute()
                logging.info(f"Successfully verified write access to document {doc_id}")
                return True
            except Exception as write_error:
                logging.error(f"No write access to document {doc_id}. Error: {str(write_error)}. Please ensure the service account has editor rights.")
                return False
        except Exception as e:
            logging.error(f"Cannot access document {doc_id}: {str(e)}. Please ensure the service account has editor rights.")
            return False

    def clear_all_processed_status(self):
        """Clear the processed status from all files. Use with caution!"""
        print(f"[TEST MODE] Clearing '{self.PROCESSED_PROPERTY}' property from all accessible files...")
        query = f"properties has {{ key='{self.PROCESSED_PROPERTY}' and value='true' }}"
        page_token = None
        cleared_count = 0
        while True:
            response = self.service.files().list(
                q=query,
                fields='nextPageToken, files(id, name, properties)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token
            ).execute()
            files = response.get('files', [])
            for file in files:
                file_id = file['id']
                file_name = file.get('name', 'unknown')
                try:
                    self.service.files().update(
                        fileId=file_id,
                        body={'properties': {self.PROCESSED_PROPERTY: ""}},
                        supportsAllDrives=True
                    ).execute()
                    print(f"Cleared '{self.PROCESSED_PROPERTY}' for file: {file_name} ({file_id})")
                    cleared_count += 1
                except Exception as e:
                    print(f"Failed to clear property for file {file_name} ({file_id}): {e}")
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        print(f"[TEST MODE] Total files cleared: {cleared_count}")

    def mark_all_with_title_as_processed(self, file_title):
        """Mark all files with the given title as processed by setting the custom property."""
        query = f"name = '{file_title.replace("'", "\\'")}' and mimeType = 'application/vnd.google-apps.document' and trashed = false"
        page_token = None
        marked_count = 0
        while True:
            response = self.service.files().list(
                q=query,
                fields='nextPageToken, files(id, name, properties)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token
            ).execute()
            files = response.get('files', [])
            for file in files:
                file_id = file['id']
                try:
                    self.service.files().update(
                        fileId=file_id,
                        body={'properties': {self.PROCESSED_PROPERTY: 'true'}},
                        supportsAllDrives=True
                    ).execute()
                    logging.info(f"Marked file {file_id} (title: {file_title}) as processed ({self.PROCESSED_PROPERTY}=true)")
                    marked_count += 1
                except Exception as e:
                    logging.error(f"Failed to mark file {file_id} (title: {file_title}) as processed: {e}")
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        logging.info(f"Marked {marked_count} files with title '{file_title}' as processed.") 