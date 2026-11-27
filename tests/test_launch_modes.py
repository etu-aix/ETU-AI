"""Network launch profiles keep normal mode private and opt in to exceptions."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
AI_SERVER = ROOT / "ai-server"
sys.path.insert(0, str(AI_SERVER))

import configs
from run_launcher import configure_launch_environment, start_server, start_companion


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

    def test_server_uses_project_environment_when_available(self):
        expected = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        with patch('run_launcher.Path.is_file',return_value=True), patch('run_launcher.subprocess.Popen') as launch:
            start_server('local')
        self.assertEqual(launch.call_args.args[0][0],str(expected))
        self.assertEqual(launch.call_args.kwargs['env']['ETU_LAUNCH_MODE'],'local')

    @unittest.skipUnless(os.name == 'nt', 'Python livré par l’installateur Windows')
    def test_server_uses_bundled_python_when_no_venv_exists(self):
        bundled = ROOT / 'python' / 'python.exe'
        with patch('run_launcher.Path.is_file', side_effect=[False, True, True]), patch('run_launcher.subprocess.Popen') as launch:
            start_server('local')
        self.assertEqual(launch.call_args.args[0][0], str(bundled))

    @unittest.skipUnless(os.name == 'nt', 'Lanceur compilé pour Windows')
    def test_frozen_launcher_prefers_packaged_runtime_over_legacy_venv(self):
        bundled = ROOT / 'python' / 'python.exe'
        with patch('run_launcher.sys.frozen', True, create=True), patch('run_launcher.Path.is_file', return_value=True), patch('run_launcher.subprocess.Popen') as launch:
            start_server('local')
        self.assertEqual(launch.call_args.args[0][0], str(bundled))
        self.assertEqual(launch.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_server_retains_current_virtual_environment(self):
        with patch('run_launcher.Path.is_file',return_value=False), patch('run_launcher.sys.prefix', 'virtual-env'), patch('run_launcher.sys.base_prefix', 'system-python'), patch('run_launcher.subprocess.Popen') as launch:
            start_server('normal')
        self.assertEqual(launch.call_args.args[0][0],sys.executable)

    def test_server_rejects_system_python_without_project_environment(self):
        with patch('run_launcher.Path.is_file', return_value=False), patch('run_launcher.sys.prefix', 'system-python'), patch('run_launcher.sys.base_prefix', 'system-python'), patch('run_launcher.subprocess.Popen') as launch:
            with self.assertRaises(RuntimeError):
                start_server('normal')
            launch.assert_not_called()

    def test_companion_never_uses_system_python(self):
        with patch('run_launcher.Path.is_file', return_value=False), patch('run_launcher.sys.prefix', 'system-python'), patch('run_launcher.sys.base_prefix', 'system-python'), patch('run_launcher.subprocess.run') as probe:
            self.assertFalse(start_companion())
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
