import os
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from meeting_auth import default_credentials
from meeting_ready_worker import _id_token
from prepare_vm_config import build_config
from vm_runner import Runner, load_runtime_config


class VMRunnerTests(unittest.TestCase):
    def test_launch_passes_same_source_key_and_prevents_duplicate_or_extra_analysis(self):
        process = Mock()
        process.poll.return_value = None
        process.pid = 12345
        spawn = Mock(return_value=process)
        runner = Runner(spawn)
        self.addCleanup(runner.pool.shutdown)
        key = 'a' * 64
        self.assertEqual(runner.launch('analysis', key), 'vm/analysis/' + key)
        self.assertEqual(spawn.call_args.kwargs['env']['ANALYSIS_STATE_ID'], key)
        with self.assertRaises(RuntimeError):
            runner.launch('analysis', key)
        with self.assertRaises(RuntimeError):
            runner.launch('analysis', 'b' * 64)
        process.poll.return_value = 0
        runner.reap()
        runner.launch('analysis', 'b' * 64)

    def test_invalid_identity_cannot_be_passed_to_a_worker(self):
        runner = Runner(Mock())
        self.addCleanup(runner.pool.shutdown)
        with self.assertRaises(ValueError):
            runner.launch('archive', '../../invalid')

    def test_checker_dispatches_to_vm_workers_not_cloud_jobs(self):
        from flask import jsonify
        process = Mock()
        process.poll.return_value = None
        spawn = Mock(return_value=process)
        runner = Runner(spawn)
        self.addCleanup(runner.pool.shutdown)
        def checker(launch_archive, launch_analysis):
            launch_archive('cloud-job', 'a' * 64)
            launch_analysis('cloud-job', 'b' * 64)
            return jsonify({'ready_count': 1})
        with patch('vm_runner.archive_checker.check_meetings', side_effect=checker):
            runner.check()
        self.assertEqual(spawn.call_count, 2)
        self.assertTrue(spawn.call_args_list[0].args[0][-1].endswith('archive_job.py'))
        self.assertTrue(spawn.call_args_list[1].args[0][-1].endswith('analysis_job.py'))
        self.assertEqual(spawn.call_args_list[0].kwargs['env']['ARCHIVE_STATE_ID'], 'a' * 64)
        self.assertEqual(spawn.call_args_list[1].kwargs['env']['ANALYSIS_STATE_ID'], 'b' * 64)

    def test_long_running_archive_is_stopped_before_its_lease_expires(self):
        process = Mock()
        process.poll.return_value = None
        process.pid = 12345
        clock = Mock(return_value=0)
        runner = Runner(Mock(return_value=process), clock=clock)
        self.addCleanup(runner.pool.shutdown)
        runner.launch('archive', 'a' * 64)
        clock.return_value = 12 * 3600 - 60
        with patch('vm_runner.os.killpg') as kill_group:
            runner.reap()
        kill_group.assert_called_once_with(12345, 9)
        with self.assertRaises(RuntimeError):
            runner.launch('archive', 'a' * 64)

    def test_vm_impersonation_uses_metadata_and_preserves_project(self):
        with patch.dict(os.environ, {'MEETING_VM_SERVICE_ACCOUNT': 'drive@example.test',
                                     'GOOGLE_CLOUD_PROJECT': 'project'}, clear=True), \
             patch('meeting_auth.Credentials') as metadata, \
             patch('meeting_auth.impersonated_credentials.Credentials') as impersonated:
            credentials, project = default_credentials(['scope'])
        self.assertEqual(project, 'project')
        self.assertIs(credentials, impersonated.return_value)
        self.assertEqual(impersonated.call_args.kwargs['target_principal'], 'drive@example.test')
        self.assertEqual(impersonated.call_args.kwargs['target_scopes'], ['scope'])
        self.assertIs(impersonated.call_args.kwargs['source_credentials'], metadata.return_value)

    def test_cloud_auth_path_is_unchanged(self):
        with patch.dict(os.environ, {}, clear=True), patch('meeting_auth.google.auth.default') as auth:
            default_credentials(['scope'])
        auth.assert_called_once_with(scopes=['scope'])

    def test_vm_delivery_uses_audience_bound_impersonated_identity(self):
        import base64
        import json
        claims = {'aud': 'https://receiver.test/event', 'email': 'drive@example.test'}
        part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        token = 'header.' + part + '.signature'
        config = Mock(url=claims['aud'], service_account_email=claims['email'])
        with patch.dict(os.environ, {'MEETING_VM_SERVICE_ACCOUNT': claims['email']}), \
             patch('meeting_ready_worker.vm_id_token', return_value=token) as mint:
            self.assertEqual(_id_token(config), token)
            mint.assert_called_once_with(claims['aud'])

    def test_runtime_loads_secret_references_without_putting_values_in_config(self):
        config = {'variables': {'GOOGLE_CLOUD_PROJECT': 'project'},
                  'secrets': {'GOOGLE_AI_API_KEY': 'projects/project/secrets/key/versions/latest'}}
        session = Mock()
        session.get.return_value.json.return_value = {'payload': {'data': 'c2VjcmV0'}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            load_runtime_config(path, session)
            self.assertEqual(os.environ['GOOGLE_AI_API_KEY'], 'secret')
            self.assertNotIn('secret"', path.read_text())
        session.get.assert_called_once_with(
            'https://secretmanager.googleapis.com/v1/projects/project/secrets/key/versions/latest:access', timeout=20)

    def test_runtime_rejects_non_secret_manager_urls(self):
        config = {'variables': {}, 'secrets': {'KEY': 'http://attacker.test'}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            session = Mock()
            with self.assertRaises(ValueError):
                load_runtime_config(path, session)
            session.get.assert_not_called()

    def test_cloud_config_requires_same_identity_and_matching_settings(self):
        def resource(value, account='drive@example.test'):
            return {'spec': {'template': {'spec': {'serviceAccountName': account,
                    'containers': [{'env': [{'name': 'SHARED', 'value': value},
                        {'name': 'API_KEY', 'valueFrom': {'secretKeyRef': {'name': 'key', 'key': 'latest'}}}]}]}}}}
        config = build_config([resource('same'), resource('same')], 'project', 'drive@example.test')
        self.assertEqual(config['secrets']['API_KEY'], 'projects/project/secrets/key/versions/latest')
        self.assertNotIn('API_KEY', config['variables'])
        with self.assertRaises(ValueError):
            build_config([resource('one'), resource('two')], 'project', 'drive@example.test')
        with self.assertRaises(ValueError):
            build_config([resource('one', 'other@example.test')], 'project', 'drive@example.test')
