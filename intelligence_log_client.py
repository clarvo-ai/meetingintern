"""Best-effort, bounded delivery of structured Intelligence events."""
import hashlib
import json
import os
import queue
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        self.items = queue.Queue(maxsize=100)
        self.lock = threading.Lock()
        self.thread = None

    def emit(self, code, *, key, count=None):
        """Queue one event; key is hashed locally and never sent in its original form."""
        try:
            url = os.environ.get('CLARVO_LOGS_URL', '')
            secret_file = os.environ.get('CLARVO_LOGS_TOKEN_FILE', '')
            parsed = urlsplit(url)
            if not secret_file or not ((parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password)
                                       or (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost'))):
                return False
            if not isinstance(key, str) or not key or len(key) > 512:
                return False
            digest = hashlib.sha256((code + '\0' + key).encode()).hexdigest()
            event_id = str(uuid.UUID(digest[:32]))
            payload = {'id': event_id, 'code': code}
            if count is not None:
                payload['count'] = count
            self.items.put_nowait((url, secret_file, payload))
            with self.lock:
                if not self.thread or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, daemon=True, name='intelligence-log')
                    self.thread.start()
            return True
        except Exception:
            return False

    def _run(self):
        opener = build_opener(_NoRedirect())
        while True:
            with self.lock:
                try:
                    url, secret_file, payload = self.items.get_nowait()
                except queue.Empty:
                    self.thread = None
                    return
            try:
                token = Path(secret_file).read_text().strip()
                if token and '\n' not in token and '\r' not in token:
                    req = Request(url, json.dumps(payload).encode(), method='POST',
                                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                    with opener.open(req, timeout=2):
                        pass
            except Exception:
                # Provider responses, URLs and tokens can contain secrets; never print them.
                pass
            finally:
                self.items.task_done()

    def flush(self, timeout=1):
        """Give a short-lived process a bounded chance to send queued events."""
        with self.lock:
            thread = self.thread
        if thread:
            thread.join(max(0, min(float(timeout), 2)))


client = Client()
