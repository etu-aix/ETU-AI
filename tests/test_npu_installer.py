"""OpenVINO release selection and NPU environment wiring."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-server"))

import configs
import llama_setup
import maia_setup


class NpuInstallerTests(unittest.TestCase):
    def test_release_and_npu_archive(self):
        self.assertEqual(configs.DEFAULT_CONFIG["llama_release_tag"], "v1.0.2")
        with patch("llama_setup.platform.system", return_value="Windows"), patch(
            "llama_setup.platform.machine", return_value="AMD64"
        ):
            self.assertEqual(
                llama_setup.get_platform_info("npu", "vulkan"),
                ("windows", "x64", "zip", ["openvino", "vulkan", "cpu"]),
            )

    def test_npu_probe_requires_openvino_and_sets_device(self):
        with patch.dict(os.environ, {"GGML_OPENVINO_DEVICE": "GPU"}), patch(
            "llama_setup.subprocess.run"
        ) as run, patch("llama_setup.list_devices", return_value=[{"id": "OPENVINO0"}]) as devices:
            run.return_value.returncode = 0
            self.assertTrue(llama_setup.test_binary_execution("llama-server", "npu"))
            self.assertEqual(run.call_args.kwargs["env"]["GGML_OPENVINO_DEVICE"], "NPU")
            self.assertEqual(devices.call_args.kwargs["env"]["GGML_OPENVINO_DEVICE"], "NPU")
            devices.return_value = [{"id": "Vulkan0"}]
            self.assertFalse(llama_setup.test_binary_execution("llama-server", "npu"))

    def test_beacon_receives_npu_environment(self):
        config = dict(configs.DEFAULT_CONFIG, _actual_backend="npu")
        with patch.dict(os.environ, {"GGML_OPENVINO_DEVICE": "GPU"}):
            env = maia_setup.managed_environment(config, ROOT / "llama-server", 12345)
        self.assertEqual(env["TARGET_DEVICE"], "NPU")
        self.assertEqual(env["GGML_OPENVINO_DEVICE"], "NPU")
        self.assertEqual(env["GGML_OPENVINO_STATEFUL_EXECUTION"], "0")
        config["_actual_backend"] = "vulkan"
        with patch.dict(os.environ, {"GGML_OPENVINO_DEVICE": "NPU"}):
            env = maia_setup.managed_environment(config, ROOT / "llama-server", 12345)
        self.assertNotIn("GGML_OPENVINO_DEVICE", env)


if __name__ == "__main__":
    unittest.main()
