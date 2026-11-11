"""Configuration contract for the legacy meeting classifier client."""
import os
import unittest
from unittest.mock import patch

from gemini_api import GeminiAPI


class GeminiModelTests(unittest.TestCase):
    def test_default_model_matches_the_supported_meeting_pipeline(self):
        with patch.dict(os.environ, {}, clear=True), patch('gemini_api.requests.Session'):
            client = GeminiAPI('test-key')
        self.assertEqual(client.model, 'gemini-3.5-flash-lite')
        self.assertTrue(client.base_url.endswith('/models/gemini-3.5-flash-lite:generateContent'))

    def test_explicit_model_remains_configurable(self):
        with patch.dict(os.environ, {'GEMINI_MODEL': 'custom-model'}, clear=True), patch('gemini_api.requests.Session'):
            client = GeminiAPI('test-key')
        self.assertEqual(client.model, 'custom-model')


if __name__ == '__main__':
    unittest.main()
