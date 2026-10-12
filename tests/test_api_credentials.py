import unittest
from types import SimpleNamespace
from unittest.mock import patch

from storage import api_credentials


class TestWindowsCredentialPrecedence(unittest.TestCase):
    def test_new_dpapi_key_replaces_stale_fallback(self):
        vault = {"mistral": "old-vault-key"}
        fallback = {"mistral": "stale-fallback-key"}

        def save_vault(values):
            vault.clear()
            vault.update(values)

        with (
            patch.object(api_credentials, "os", SimpleNamespace(name="nt")),
            patch.object(api_credentials, "_load_windows_secrets", side_effect=lambda: dict(vault)),
            patch.object(api_credentials, "_save_windows_secrets", side_effect=save_vault),
            patch.object(api_credentials, "_get_windows_fallback", side_effect=lambda provider: fallback.get(provider)),
            patch.object(api_credentials, "_delete_windows_fallback", side_effect=lambda provider: fallback.pop(provider, None)),
            patch.object(api_credentials, "_set_windows_fallback") as set_fallback,
        ):
            api_credentials.set_api_key("new-key", "mistral")

            self.assertEqual(api_credentials.get_api_key("mistral"), "new-key")
            self.assertNotIn("mistral", fallback)
            set_fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
