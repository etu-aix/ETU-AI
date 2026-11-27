"""The Windows setup must seed, then preserve, the user's engine configuration."""

import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallerConfigTests(unittest.TestCase):
    def test_setup_seeds_config_once_and_excludes_it_from_overwrites(self):
        script = (Path(__file__).resolve().parents[1] / "installer" / "setup.iss").read_text(encoding="utf-8")
        lines = [line.strip() for line in script.splitlines() if line.lstrip().startswith("Source:")]

        recursive_payload = next(line for line in lines if "release-payload\\*" in line)
        config_seed = next(line for line in lines if "release-payload\\ai-server\\config.json" in line)

        self.assertIn('Excludes: "ai-server\\config.json"', recursive_payload)
        self.assertIn('Flags: onlyifdoesntexist', config_seed)
        self.assertIn('Type: filesandordirs; Name: "{app}\\.venv"', script)
        self.assertNotIn('Name: "{app}\\storage"', script)
        self.assertNotIn('Name: "{app}\\ai-server\\models"', script)

    def test_setup_does_not_run_python_or_pip_during_installation(self):
        root = Path(__file__).resolve().parents[1]
        setup = (root / "installer" / "setup.iss").read_text(encoding="utf-8")
        workflow = (root / ".github" / "workflows" / "windows-setup.yml").read_text(encoding="utf-8")
        self.assertNotIn("installer\\prepare.py", setup)
        self.assertIn("pip install --no-deps --no-build-isolation --upgrade --target release-payload/python/Lib/site-packages", workflow)
        self.assertNotIn("--only-binary=:all:", workflow)
        self.assertIn('Filename: "{app}\\ETU-AI.exe"', setup)
        self.assertIn("PrivilegesRequired=lowest", setup)
        self.assertNotIn("[Run]", setup)
        self.assertIn("SetupIconFile=..\\assets\\etu-ai_logo.ico", setup)

    def test_windows_build_uses_exact_runtime_dependency_versions(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "windows-setup.yml").read_text(encoding="utf-8")
        lock = (root / "requirements-windows.lock.txt").read_text(encoding="utf-8")
        self.assertIn("python-version: '3.12.10'", workflow)
        self.assertIn("actions/checkout@v7", workflow)
        self.assertIn("actions/setup-python@v7", workflow)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)
        self.assertIn("pyinstaller==6.22.3", workflow)
        self.assertIn("setuptools==84.2.1", workflow)
        self.assertIn("innosetup --version=6.7.1", workflow)
        self.assertIn("-r requirements-windows.lock.txt", workflow)
        for package in ("fastapi", "uvicorn", "httpx", "keyring", "pypdf", "pillow", "cryptography", "websockets", "ddgs", "lxml", "pywebview", "pynput"):
            self.assertRegex(lock, rf"(?m)^{package}==[^\s]+$")
        self.assertRegex(
            lock,
            r"(?m)^proxy_tools @ https://files\.pythonhosted\.org/.+proxy_tools-0\.1\.0\.tar\.gz#sha256=ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010$",
        )

    def test_release_workflow_allows_optional_authenticode_and_uses_bundled_signer(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "windows-setup.yml").read_text(encoding="utf-8")

        self.assertIn("if (($env:SIGNING_PFX -and -not $env:SIGNING_PASSWORD) -or (-not $env:SIGNING_PFX -and $env:SIGNING_PASSWORD))", workflow)
        self.assertIn("Build sans Authenticode", workflow)
        self.assertGreaterEqual(workflow.count("SignerCertificate.Subject -notmatch '(?i)etu-aix'"), 2)
        self.assertIn("& release-payload/python/python.exe tools/sign_release.py", workflow)
        self.assertIn("tools/sign_release.py --check-key --private-key", workflow)
        self.assertIn('--icon "$env:GITHUB_WORKSPACE/assets/etu-ai_logo.ico"', workflow)
        self.assertIn("signature-status.txt", workflow)
        self.assertIn("validate_release_signing_key(key, release_signing_public_keys())", (root / "tools" / "sign_release.py").read_text(encoding="utf-8"))
        self.assertIn("unittest tests.test_api tests.test_app_releases tests.test_signed_updates tests.test_installer_config tests.test_launch_modes", workflow)
        self.assertIn("Smoke test per-user offline package", workflow)
        self.assertIn("Block internet access for offline smoke test", workflow)
        self.assertIn("-Action Block -RemoteAddress Internet -Profile Any", workflow)
        self.assertIn("The outbound firewall rule did not block internet access.", workflow)
        self.assertIn("Remove offline smoke firewall rule", workflow)
        self.assertIn("if: ${{ always() }}", workflow)
        self.assertIn("/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-", workflow)
        self.assertIn("'/DAppVersion=0.0.0' \"/O$oldOutput\" installer/setup.iss", workflow)
        self.assertIn("$oldSetup.FullName", workflow)
        self.assertIn("Upgrade from the previous version failed", workflow)
        self.assertIn("Get-Content -Raw $versionFile", workflow)
        self.assertIn("$env:PATH = Join-Path $env:SystemRoot 'System32'", workflow)
        self.assertIn("existing-chat-data", workflow)
        self.assertIn('{"beacon_port":12567}', workflow)
        self.assertIn("existing-model-data", workflow)
        self.assertIn("Start-Process -FilePath $launcherPath", workflow)
        self.assertIn("$env:HTTPS_PROXY = 'http://127.0.0.1:9'", workflow)
        self.assertIn("http://127.0.0.1:8900/api/status", workflow)
        smoke = workflow.split("      - name: Smoke test per-user offline package", 1)[1]
        self.assertLess(smoke.index("$env:HTTPS_PROXY = 'http://127.0.0.1:9'"), smoke.index("Start-Process -FilePath $setup.FullName"))
        firewall = workflow.index("      - name: Block internet access for offline smoke test")
        cleanup = workflow.index("      - name: Remove offline smoke firewall rule")
        signing = workflow.index("      - name: Check Ed25519 release key")
        self.assertLess(firewall, workflow.index("      - name: Smoke test per-user offline package"))
        self.assertLess(workflow.index("      - name: Smoke test per-user offline package"), cleanup)
        self.assertLess(cleanup, signing)

    def test_companion_uses_the_shared_application_icon(self):
        root = Path(__file__).resolve().parents[1]
        companion = (root / "companion.py").read_text(encoding="utf-8")
        icon = root / "assets" / "etu-ai_logo.ico"
        self.assertTrue(icon.is_file())
        self.assertIn('"assets" / "etu-ai_logo.ico"', companion)
        self.assertIn("webview.start(private_mode=False, icon=str(icon) if icon.is_file() else None)", companion)

    def test_update_supervisor_rechecks_the_signed_package_before_execution(self):
        root = Path(__file__).resolve().parents[1]
        supervisor = (root / "installer" / "update_supervisor.ps1").read_text(encoding="utf-8")
        api = (root / "api_server.py").read_text(encoding="utf-8")

        self.assertIn("[string]$ExpectedSha256", supervisor)
        self.assertIn("[long]$ExpectedSize", supervisor)
        self.assertIn("[int]$CompanionPid", supervisor)
        self.assertIn("Get-Process -Id $CompanionPid", supervisor)
        self.assertIn("Stop-Process -Id $CompanionPid", supervisor)
        self.assertIn("candidate.Path", supervisor)
        self.assertIn("[Security.Cryptography.SHA256]::Create()", supervisor)
        self.assertLess(supervisor.index("$sha256.ComputeHash($packageStream)"), supervisor.index("Start-Process -FilePath $installerPath"))
        self.assertLess(supervisor.index("Start-Process -FilePath $installerPath"), supervisor.index("$packageStream.Dispose()"))
        self.assertIn('manifest["sha256"], str(manifest["size"])', api)
        self.assertIn("companion_pid_file(BASE_DIR)", api)
        self.assertIn("str(companion_pid)", api)

    @unittest.skipUnless(os.name == "nt", "Le superviseur d’installation est un script Windows")
    def test_update_supervisor_rejects_a_package_changed_after_server_verification(self):
        root = Path(__file__).resolve().parents[1]
        supervisor_source = root / "installer" / "update_supervisor.ps1"
        with tempfile.TemporaryDirectory(prefix="etu-update-test-") as directory:
            temp = Path(directory)
            installer = temp / "etu-app-installer-corrupt.exe"
            supervisor = temp / "update-supervisor.ps1"
            app_launcher = temp / "ETU-AI.exe"
            installer.write_bytes(b"MZ!")
            supervisor.write_text(supervisor_source.read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(supervisor),
                 "0", "0", "0", str(installer), str(app_launcher), str(temp / "python.exe"),
                 str(temp / "companion.pid"), str(supervisor), "0" * 64, "3"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Installer hash changed after authentication.", result.stderr)
            self.assertFalse(installer.exists())
            self.assertFalse(supervisor.exists())

    @unittest.skipUnless(os.name == "nt", "Le superviseur d’installation est un script Windows")
    def test_update_supervisor_runs_the_verified_file_while_its_handle_is_locked(self):
        root = Path(__file__).resolve().parents[1]
        source_executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
        if not source_executable.is_file():
            self.skipTest("whoami.exe est absent de cette installation Windows")

        with tempfile.TemporaryDirectory(prefix="etu-update-test-") as directory:
            temp = Path(directory)
            installer = temp / "etu-app-installer-valid.exe"
            supervisor = temp / "update-supervisor.ps1"
            shutil.copyfile(source_executable, installer)
            expected_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
            expected_size = installer.stat().st_size
            supervisor.write_text((root / "installer" / "update_supervisor.ps1").read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(supervisor),
                 "0", "0", "0", str(installer), str(temp / "ETU-AI.exe"), str(temp / "python.exe"),
                 str(temp / "companion.pid"), str(supervisor), expected_hash, str(expected_size)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(installer.exists())
            self.assertFalse(supervisor.exists())

    @unittest.skipUnless(os.name == "nt", "Le superviseur d’installation est un script Windows")
    def test_update_supervisor_closes_the_companion_before_running_setup(self):
        root = Path(__file__).resolve().parents[1]
        source_executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
        if not source_executable.is_file():
            self.skipTest("whoami.exe est absent de cette installation Windows")

        with tempfile.TemporaryDirectory(prefix="etu-update-test-") as directory:
            temp = Path(directory)
            installer = temp / "etu-app-installer-valid.exe"
            supervisor = temp / "update-supervisor.ps1"
            companion_script = temp / "companion.py"
            companion_script.write_text("import time; time.sleep(60)\n", encoding="utf-8")
            shutil.copyfile(source_executable, installer)
            expected_hash = hashlib.sha256(installer.read_bytes()).hexdigest()
            expected_size = installer.stat().st_size
            supervisor.write_text((root / "installer" / "update_supervisor.ps1").read_text(encoding="utf-8"), encoding="utf-8")
            companion = subprocess.Popen(
                [sys.executable, str(companion_script)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(supervisor),
                     "0", "0", str(companion.pid), str(installer), str(temp / "ETU-AI.exe"), sys.executable,
                     str(temp / "companion.pid"), str(supervisor), expected_hash, str(expected_size)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(companion.poll(), "The companion process must exit before setup starts.")
                self.assertFalse(installer.exists())
                self.assertFalse(supervisor.exists())
            finally:
                if companion.poll() is None:
                    companion.terminate()
                    companion.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
