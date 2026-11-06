import unittest
from unittest.mock import Mock, patch
from archive_job import work_once


class PipelineLoggingTests(unittest.TestCase):
    def test_failure_logs_only_after_claim_released(self):
        state = Mock()
        state.claim_for_work.return_value = {}
        # A non-empty claim reaches the failing Drive initialization.
        state.claim_for_work.return_value = {'source_context_doc_ids': []}
        state.mark_retry.return_value = True
        with patch('archive_job.build', side_effect=RuntimeError('private')), \
             patch('archive_job.intelligence_log') as logged:
            with self.assertRaises(RuntimeError):
                work_once('opaque', 'destination', 'key', state, Mock())
            logged.emit.assert_called_once()
            self.assertEqual(logged.emit.call_args.args, ('meeting_archive_failed',))
            details = logged.emit.call_args.kwargs['details']
            self.assertEqual(details['exception']['type'], 'RuntimeError')
            self.assertEqual(details['retry_after_seconds'], 900)
            self.assertNotIn('private', str(details))
            state.mark_retry.assert_called_once()
            logged.reset_mock()
            state.mark_retry.return_value = False
            with self.assertRaises(RuntimeError):
                work_once('opaque', 'destination', 'key', state, Mock())
            logged.emit.assert_not_called()


if __name__ == '__main__': unittest.main()
