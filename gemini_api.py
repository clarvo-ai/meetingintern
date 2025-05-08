import requests
import logging
import time
import json
from tenacity import retry, stop_after_attempt, wait_exponential
import os

class GeminiAPI:
    def __init__(self, api_key):
        """Initialize Gemini API client using REST transport."""
        if not api_key:
            raise ValueError("API key cannot be empty")
            
        self.api_key = api_key
        self.model = os.getenv('GEMINI_MODEL', 'gemini-2.0-flash')
        self.base_url = f"https://generativelanguage.googleapis.com/v1/models/{self.model}:generateContent"
        self.requests_in_minute = 0
        self.last_request_time = time.time()
        self.MAX_CHUNK_SIZE = 30000
        
        # Configure session for REST calls
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'x-goog-api-key': self.api_key
        })
        
        # Test the connection
        try:
            self._generate_with_retry("Test connection.")
            logging.info("Successfully initialized Gemini API with REST transport")
        except Exception as e:
            if "invalid api key" in str(e).lower():
                raise ValueError("Invalid Gemini API key provided")
            elif "permission denied" in str(e).lower():
                raise ValueError("API key does not have permission to access Gemini API")
            else:
                logging.error(f"Failed to initialize Gemini API: {str(e)}")
                raise

    def _check_rate_limit(self):
        """Implement rate limiting to stay within API quotas."""
        current_time = time.time()
        if current_time - self.last_request_time >= 60:
            self.requests_in_minute = 0
            self.last_request_time = current_time
        
        if self.requests_in_minute >= 55:
            sleep_time = 60 - (current_time - self.last_request_time)
            if sleep_time > 0:
                logging.info(f"Rate limit approaching, waiting {sleep_time:.2f} seconds")
                time.sleep(sleep_time)
                self.requests_in_minute = 0
                self.last_request_time = time.time()
        
        self.requests_in_minute += 1

    def _chunk_content(self, content):
        """Split content into manageable chunks."""
        if len(content) <= self.MAX_CHUNK_SIZE:
            return [content]
        
        chunks = []
        current_chunk = []
        current_size = 0
        
        for line in content.split('\n'):
            line_size = len(line) + 1
            if current_size + line_size > self.MAX_CHUNK_SIZE:
                if current_chunk:
                    chunks.append('\n'.join(current_chunk))
                current_chunk = [line]
                current_size = line_size
            else:
                current_chunk.append(line)
                current_size += line_size
        
        if current_chunk:
            chunks.append('\n'.join(current_chunk))
        
        return chunks

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
    def _generate_with_retry(self, prompt, generation_config=None):
        """Make API call with retry logic."""
        self._check_rate_limit()
        try:
            if isinstance(prompt, str):
                chunks = self._chunk_content(prompt)
                if len(chunks) > 1:
                    logging.info(f"Content split into {len(chunks)} chunks")
                    prompt = chunks[0]
            
            generation_config = generation_config or {
                'temperature': 0.7,
                'top_p': 0.8,
                'top_k': 40,
                'max_output_tokens': 2048,
            }
            
            payload = {
                "contents": [{
                    "parts": [{
                        "text": prompt
                    }]
                }],
                "generationConfig": generation_config
            }
            
            response = self.session.post(
                self.base_url,
                json=payload
            )
            
            if response.status_code == 429:
                logging.warning("Rate limit hit, retrying with backoff...")
                time.sleep(5)
                raise Exception("Rate limit exceeded")
                
            response.raise_for_status()
            result = response.json()
            
            if not result.get("candidates"):
                raise Exception("Empty response from Gemini API")
                
            return result["candidates"][0]["content"]["parts"][0]["text"].strip()
            
        except Exception as e:
            logging.error(f"Error generating content: {str(e)}")
            raise

    def determine_meeting_type(self, transcript_text, document_name=None):
        """Determine meeting type from transcript content using Gemini API."""
        try:
            # Create a more concise prompt for classification
            # Customize these meeting types based on your organization's needs
            prompt = """
            Classify the meeting into one of these types:
            - Daily Team Meeting (morning, team plans, 30-45min, daily coordination)
            - Investor Meeting
            - Client Meeting
            - HR & Recruitment
            - User Research Meeting
            - Product Development Meeting
            - Other
            
            Return ONLY the category name.
            """
            
            if document_name:
                prompt += f"\n\nDocument name: {document_name}\n"
            
            # Use only the first part of transcript for classification
            content_chunk = self._chunk_content(transcript_text)[0]
            prompt += f"\nTranscript excerpt:\n{content_chunk}"
            
            result = self._generate_with_retry(prompt)
            
            # Update these categories to match your organization's meeting types
            valid_categories = {
                "Daily Team Meeting", "Investor Meeting", "Client Meeting",
                "HR & Recruitment", "User Research Meeting", 
                "Product Development Meeting", "Other"
            }
            
            return result if result in valid_categories else "Other"
            
        except Exception as e:
            logging.error(f"Failed to determine meeting type: {str(e)}")
            return "Other"

    def summarize_transcript(self, transcript_text):
        """Summarize meeting transcript using Gemini API."""
        try:
            chunks = self._chunk_content(transcript_text)
            if len(chunks) == 1:
                # Single chunk, process normally
                prompt = """
                Provide a concise summary focusing on:
                - Key discussion points
                - Important decisions made
                - Action items or next steps
                - Project updates
                """
                prompt += f'\n\nTranscript:\n{chunks[0]}'
                return self._generate_with_retry(prompt)
            else:
                # Multiple chunks, process each and combine
                logging.info(f"Processing transcript in {len(chunks)} chunks")
                summaries = []
                for i, chunk in enumerate(chunks):
                    prompt = f"""
                    Summarize part {i+1}/{len(chunks)} of the transcript, focusing on:
                    - Key points
                    - Decisions
                    - Action items
                    """
                    prompt += f'\n\nTranscript part {i+1}:\n{chunk}'
                    summary = self._generate_with_retry(prompt)
                    if summary:
                        summaries.append(summary)
                
                # Combine summaries
                if summaries:
                    combined_prompt = "Combine these summary parts into a coherent final summary:\n\n"
                    combined_prompt += "\n\n".join(f"Part {i+1}:\n{summary}" for i, summary in enumerate(summaries))
                    return self._generate_with_retry(combined_prompt)
                
            return None
            
        except Exception as e:
            logging.error(f"Failed to summarize transcript: {str(e)}")
            return None

    def generate_chat_summary(self, transcript_text, meeting_time=None):
        """Generate a concise, action-oriented summary for Google Chat."""
        try:
            prompt = """
            Summarize this meeting for a Google Chat message, output should be a ready to go message with styling applied and emojis if needed. Focus on:
            - Key decisions
            - Action items
            - Main discussion points
            - Plans for the day/week (if applicable)
            - Keep it concise and actionable for the team.
            - Seperate to instructions and updates per person referred to in the transcript.
            - Try to keep it concise, but don't leave out any key information.
            - Split it into "Updates" and "Action Points" sections.
            - Actual Unicode emojis (e.g., 👍, 🔥, 📌) at the start of each section and for highlights.
            - Bullet points using '-' or '•' (not '*').
            - Use *bold* for section headers and important points.
            - No Markdown that Google Chat does not support.
            - No :emoji_name: shortcodes, only real emoji characters.
            """
            if meeting_time:
                prompt += f"\nMeeting time: {meeting_time}"
            prompt += f"\n\nTranscript:\n{transcript_text}"  # Limit to first 3000 chars for brevity
            logging.info(f"Gemini chat summary prompt: {repr(prompt)}")
            result = self._generate_with_retry(prompt)
            logging.info(f"Gemini chat summary result: {repr(result)}")
            return result.strip() if result else None
        except Exception as e:
            logging.error(f"Failed to generate chat summary: {str(e)}")
            return None

    def generate_user_validation_summary(self, transcript_text, meeting_time=None):
        """Generate a specialized summary for user validation meetings."""
        try:
            prompt = """
            Create a user validation meeting summary for a Google Chat message, output should be a ready to go message with styling applied and emojis if needed, focusing on:
            - User feedback and pain points
            - Features discussed, both existing and new/upcoming
            - User behavior and usage patterns
            - Validation of assumptions
            - Key insights and learnings
            - Next steps and follow-up actions
            
            Format the output with:
            - Clear sections with emojis
            - Highlight critical feedback
            - Highlight development tasks or possible features/methods discussed 
            - Include any specific quotes from users
            - Add a "Next Steps" section with actionable items

            - Actual Unicode emojis (e.g., 👍, 🔥, 📌) at the start of each section and for highlights.
            - Bullet points using '-' or '•' (not '*').
            - Use *bold* for section headers and important points.
            - No Markdown that Google Chat does not support.
            - No :emoji_name: shortcodes, only real emoji characters.
            
            Keep it comprehensive, focusing on development areas, and don't leave out any key information or important discussions.
            """
            if meeting_time:
                prompt += f"\nMeeting time: {meeting_time}"
            prompt += f"\n\nTranscript:\n{transcript_text}"  # Limit to first 3000 chars for brevity
            logging.info(f"Gemini user validation summary prompt: {repr(prompt)}")
            result = self._generate_with_retry(prompt)
            logging.info(f"Gemini user validation summary result: {repr(result)}")
            return result.strip() if result else None
        except Exception as e:
            logging.error(f"Failed to generate user validation summary: {str(e)}")
            return None 