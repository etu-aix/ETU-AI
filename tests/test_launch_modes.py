"""Network launch profiles keep normal mode private and opt in to exceptions."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AI_SERVER = ROOT / "ai-server"
sys.path.insert(0, str(AI_SERVER))

import configs
from run_launcher import configure_launch_environment


class LaunchModeTests(unittest.TestCase):
    def test_normal_and_local_keep_beacon_on_loopback(self):
        for mode in ("normal", "local"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"ETU_LAUNCH_MODE": mode}):
                self.assertIn(configs.load_config()["beacon_host"], {"127.0.0.1", "localhost", "::1"})

    def test_server_mode_exposes_only_beacon_wildcard_bind(self):
        with patch.dict(os.environ, {"ETU_LAUNCH_MODE": "server"}):
            self.assertEqual(configs.load_config()["beacon_host"], "0.0.0.0")

    def test_launcher_environment_is_explicit_per_mode(self):
        base = {"KEEP_ME": "yes", "ETU_ALLOW_LOCALHOST_ORIGINS": "1"}
        normal = configure_launch_environment(base, "normal")
        local = configure_launch_environment(base, "local")
        server = configure_launch_environment(base, "server")
        self.assertEqual(normal["ETU_ALLOW_LOCALHOST_ORIGINS"], "0")
        self.assertEqual(local["ETU_ALLOW_LOCALHOST_ORIGINS"], "1")
        self.assertEqual(server["ETU_ALLOW_LOCALHOST_ORIGINS"], "0")
        self.assertEqual(local["KEEP_ME"], "yes")
        with self.assertRaises(ValueError):
            configure_launch_environment(base, "anything-else")

    def test_invalid_mode_fails_closed(self):
        with patch.dict(os.environ, {"ETU_LAUNCH_MODE": "public"}), self.assertRaises(ValueError):
            configs.load_config()


if __name__ == "__main__":
    unittest.main()
