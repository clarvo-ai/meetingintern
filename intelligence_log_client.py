"""Best-effort, bounded delivery of structured Intelligence events."""
import hashlib
import json
import os
import queue
import re
import traceback
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


_SOURCE = re.compile(r'^(?:[0-9a-f]{32}|[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$')
_SAFE_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
_SAFE_FILE = re.compile(r'^[A-Za-z0-9_.-]{1,100}\.py$')
_SAFE_FUNCTION = re.compile(r'^[A-Za-z0-9_<>]{1,80}$')
_SAFE_REASON = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
_SAFE_EXECUTION = re.compile(r'^[a-z][a-z0-9-]{0,62}$')


def diagnostics(*, stage, source=None, error=None, retry_after_seconds=None, reason=None):
    """Capture stack structure and safe provider status without error text or locals."""
    result = {'stage': stage}
    if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,40}', reason):
        result['reason'] = reason
    if isinstance(source, str) and _SOURCE.fullmatch(source):
        result['source'] = source
    execution = os.environ.get('CLOUD_RUN_EXECUTION', '')
    if _SAFE_EXECUTION.fullmatch(execution):
        result['execution'] = execution
    if type(retry_after_seconds) is int and 0 <= retry_after_seconds <= 86400:
        result['retry_after_seconds'] = retry_after_seconds
    if error is not None:
        seen, current, causes, budget = set(), error, [], 80
        while current is not None and id(current) not in seen and len(causes) < 5:
            seen.add(id(current))
            error_type = type(current).__name__
            item = {'type': error_type if _SAFE_NAME.fullmatch(error_type) else 'Exception'}
            code = getattr(current, 'code', None)
            if type(code) is int and 100 <= code <= 599:
                item['code'] = code
            reason = getattr(current, 'status', None)
            if isinstance(reason, str) and _SAFE_REASON.fullmatch(reason):
                item['reason'] = reason
            frames = []
            for frame in traceback.extract_tb(current.__traceback__):
                file = os.path.basename(frame.filename)
                if _SAFE_FILE.fullmatch(file) and _SAFE_FUNCTION.fullmatch(frame.name) and 0 < frame.lineno <= 10000000:
                    frames.append({'file': file, 'line': frame.lineno, 'function': frame.name})
            if len(frames) > budget:
                item['frames_omitted'] = len(frames) - budget
                frames = frames[-budget:] if budget else []
            item['frames'] = frames
            budget -= len(frames)
            causes.append(item)
            current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        result['exception'] = causes[0]
        if len(causes) > 1:
            result['causes'] = causes[1:]
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self):
        self.items = queue.Queue(maxsize=100)
        self.lock = threading.Lock()
        self.thread = None

    def emit(self, code, *, key, count=None, details=None):
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
            if details is not None:
                payload['details'] = details
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
