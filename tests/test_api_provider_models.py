import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import api_server


class TestProviderModelsEndpoint(unittest.TestCase):
    def setUp(self):
        self.request = api_server.ProviderModelsRequest(
            provider="mistral",
            base_url="https://api.mistral.ai/v1",
        )
        self.storage_patch = patch.object(api_server.storage, "get_setting", return_value={})
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.key_patch = patch.object(api_server, "get_api_key", return_value="test-key")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    def test_mistral_models_request_uses_bearer_auth(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"data": [{"id": "mistral-small-latest"}]}).encode()
        with patch("api_server.urllib.request.urlopen", return_value=response) as urlopen:
            result = api_server.list_provider_models(self.request, origin=None)

        self.assertEqual(result, {"models": ["mistral-small-latest"]})
        outgoing = urlopen.call_args.args[0]
        self.assertEqual(outgoing.full_url, "https://api.mistral.ai/v1/models")
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer test-key")

    def test_unauthorized_key_returns_actionable_error(self):
        error = HTTPError(
            "https://api.mistral.ai/v1/models",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b"{}"),
        )
        with patch("api_server.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(api_server.HTTPException) as raised:
                api_server.list_provider_models(self.request, origin=None)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("401", raised.exception.detail)
        self.assertIn("Bearer", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
