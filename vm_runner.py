"""Single-VM scheduler and child supervisor using the existing Firestore claims."""
import concurrent.futures
import base64
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import archive_checker
from google.auth.transport.requests import AuthorizedSession
from meeting_auth import default_credentials

LOGGER = logging.getLogger(__name__)


def load_runtime_config(path, session=None):
    """Read protected settings and resolve Secret Manager references without logging values."""
    with open(path, encoding='utf-8') as stream:
        config = json.load(stream)
    variables, secrets = config.get('variables'), config.get('secrets')
    if not isinstance(variables, dict) or not isinstance(secrets, dict):
        raise ValueError('Invalid VM runtime configuration')
    for name, value in variables.items():
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) or not isinstance(value, str):
            raise ValueError('Invalid VM environment setting')
        os.environ[name] = value
    if session is None:
        session = AuthorizedSession(default_credentials()[0])
    for name, resource in secrets.items():
        if (not re.fullmatch(r'[A-Z][A-Z0-9_]*', name) or not isinstance(resource, str) or
                not re.fullmatch(r'projects/[a-zA-Z0-9_-]+/secrets/[a-zA-Z0-9_-]+/versions/(?:latest|[0-9]+)', resource)):
            raise ValueError('Invalid VM secret reference')
        response = session.get('https://secretmanager.googleapis.com/v1/' + resource + ':access', timeout=20)
        response.raise_for_status()
        os.environ[name] = base64.b64decode(response.json()['payload']['data'], validate=True).decode('utf-8')


class Runner:
    def __init__(self, spawn=subprocess.Popen, clock=time.monotonic):
        self.spawn = spawn
        self.clock = clock
        self.children = {}
        self.lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.futures = {}
        self.due = {'check': 0, 'artifacts': 0, 'delivery': 0}
        self.periods = {'check': 900, 'artifacts': 900, 'delivery': 300}

    def reap(self):
        with self.lock:
            for ident, (process, deadline) in list(self.children.items()):
                result = process.poll()
                if result is not None:
                    LOGGER.info('Meeting worker %s exited with status %s', ident[0], result)
                    del self.children[ident]
                elif self.clock() >= deadline:
                    # Stop before its Firestore lease expires, so a replacement
                    # cannot overlap a still-running local worker.
                    LOGGER.warning('Meeting worker %s exceeded its time limit', ident[0])
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def launch(self, kind, key=None):
        if kind not in ('archive', 'analysis', 'delivery'):
            raise ValueError('Invalid worker kind')
        if key is not None and (len(key) != 64 or any(c not in '0123456789abcdef' for c in key)):
            raise ValueError('Invalid meeting source key')
        with self.lock:
            limit = {'archive': 5, 'analysis': 1, 'delivery': 1}[kind]
            if (kind, key) in self.children or sum(ident[0] == kind for ident in self.children) >= limit:
                raise RuntimeError('VM worker capacity reached')
            env = os.environ.copy()
            if kind == 'archive':
                env['ARCHIVE_STATE_ID'] = key
            elif kind == 'analysis':
                env['ANALYSIS_STATE_ID'] = key
            script = {'archive': 'archive_job.py', 'analysis': 'analysis_job.py',
                      'delivery': 'meeting_ready_worker.py'}[kind]
            process = self.spawn([sys.executable, str(Path(__file__).with_name(script))],
                                 env=env, start_new_session=True)
            timeout = {'archive': 12 * 3600 - 60, 'analysis': 2 * 3600 - 60,
                       'delivery': 30 * 60}[kind]
            self.children[(kind, key)] = (process, self.clock() + timeout)
            return 'vm/' + kind + '/' + (key or 'outbox')

    def check(self):
        with archive_checker.app.app_context():
            result = archive_checker.check_meetings(
                launch_archive=lambda _job, key: self.launch('archive', key),
                launch_analysis=lambda _job, key: self.launch('analysis', key))
            response, status = result if isinstance(result, tuple) else (result, 200)
            LOGGER.info('Meeting discovery completed with status %s', status)
            if status != 200:
                raise RuntimeError('Meeting discovery needs attention')
            return response

    def artifacts(self):
        with archive_checker.app.app_context():
            result = archive_checker.auto_artifacts_upcoming()
            _, status = result if isinstance(result, tuple) else (result, 200)
            LOGGER.info('Premeeting settings completed with status %s', status)
            if status != 200:
                raise RuntimeError('Premeeting settings need attention')

    def tick(self, now):
        self.reap()
        for kind in ('check', 'artifacts', 'delivery'):
            previous = self.futures.get(kind)
            if previous and previous.done():
                try:
                    previous.result()
                except Exception:
                    LOGGER.warning('Meeting %s cycle failed', kind)
                del self.futures[kind]
            if now < self.due[kind] or kind in self.futures:
                continue
            self.due[kind] = now + self.periods[kind]
            if kind == 'delivery':
                try:
                    self.launch('delivery')
                except RuntimeError:
                    LOGGER.warning('Meeting delivery is already running')
            else:
                self.futures[kind] = self.pool.submit(getattr(self, kind))


def main():
    logging.basicConfig(level=logging.INFO)
    load_runtime_config(os.environ['MEETING_VM_CONFIG'])
    lock_path = Path(os.environ['MEETING_VM_LOCK_PATH'])
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = Runner()
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        while not stop.is_set():
            runner.tick(time.monotonic())
            stop.wait(1)
        runner.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
