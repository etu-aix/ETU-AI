"""The Windows setup must seed, then preserve, the user's engine configuration."""

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


if __name__ == "__main__":
    unittest.main()
