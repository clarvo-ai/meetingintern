import requests
import logging
import os
from datetime import datetime

class ChatAPI:
    def __init__(self, webhook_url=None):
        """Initialize the Chat API client."""
        self.webhook_url = webhook_url or os.getenv('CHAT_WEBHOOK_URL')
        if not self.webhook_url:
            raise ValueError("CHAT_WEBHOOK_URL is required")
        
        # Determine platform from webhook URL
        self.platform = self._detect_platform()
        logging.info(f"ChatAPI initialized successfully for {self.platform}")

    def _detect_platform(self):
        """Detect chat platform from webhook URL."""
        url = self.webhook_url.lower()
        if 'slack.com' in url:
            return 'slack'
        elif 'discord.com' in url:
            return 'discord'
        elif 'chat.googleapis.com' in url:
            return 'google_chat'
        else:
            return 'unknown'

    def _format_message(self, message, platform=None):
        """Format message according to platform requirements."""
        platform = platform or self.platform
        
        if platform == 'slack':
            return {'text': message}
        elif platform == 'discord':
            return {'content': message}
        elif platform == 'google_chat':
            return {'text': message}
        else:
            # Default to plain text
            return {'text': message}

    def send_daily_meeting_summary(self, meetings):
        """Send a summary of today's meetings to the chat channel."""
        try:
            if not meetings:
                logging.info("No daily meetings to report")
                return True

            # Create the message
            today = datetime.now().strftime("%Y-%m-%d")
            message = f"*Meeting Summary for {today}*\n\n"
            
            for meeting in meetings:
                # Extract meeting details
                title = meeting.get('name', 'Untitled Meeting')
                summary = meeting.get('summary', 'No summary available')
                
                # Add to message
                message += f"📝 *{title}*\n"
                message += f"{summary}\n\n"

            # Format message for platform
            payload = self._format_message(message)
            
            # Send to chat platform
            response = requests.post(
                self.webhook_url,
                json=payload
            )
            
            if response.status_code == 200:
                logging.info(f"Successfully sent meeting summary to {self.platform}")
                return True
            else:
                logging.error(f"Failed to send message to {self.platform}. Status code: {response.status_code}, response: {response.text}")
                return False
                
        except Exception as e:
            logging.error(f"Failed to send chat message: {str(e)}")
            return False

    def send_message(self, message):
        """Send a message to the chat channel."""
        try:
            payload = self._format_message(message)
            response = requests.post(
                self.webhook_url,
                json=payload
            )
            response.raise_for_status()
            logging.info(f"Message sent successfully to {self.platform}")
            return True
        except Exception as e:
            logging.error(f"Failed to send message: {str(e)}")
            return False 