import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parent


class TestSpotifyConfiguration(unittest.TestCase):
    def test_headless_spotify_disables_mpris(self):
        with (ROOT / "spotifyd" / "spotifyd.conf").open("rb") as config_file:
            config = tomllib.load(config_file)
        self.assertIs(config["global"]["use_mpris"], False)


class TestAudioMixerConfiguration(unittest.TestCase):
    def test_ducking_default_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory)
            for name, script in {
                "chmod": "#!/bin/sh\nexit 0\n",
                "pulseaudio": "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
            }.items():
                executable = bindir / name
                executable.write_text(script)
                executable.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = str(bindir) + os.pathsep + environment["PATH"]
            environment["ORION_PLAYBACK_DEVICE"] = "null"
            for configured, expected in ((None, "-16dB"), ("-10dB", "-10dB")):
                with self.subTest(configured=configured):
                    environment.pop("ORION_DUCK_VOLUME", None)
                    if configured is not None:
                        environment["ORION_DUCK_VOLUME"] = configured
                    result = subprocess.run(
                        ["sh", str(ROOT / "audio" / "start-audio.sh")],
                        env=environment, capture_output=True, text=True, check=True,
                    )
                    self.assertIn(f"ducking_roles=music volume={expected}", result.stdout)


class TestSpotifyScripts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        (self.root / "spotifyd").mkdir()
        bindir = self.root / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  'compose config --environment') grep '^ORION_[UG]ID=' .env || : ;;\n"
            "  'compose --profile spotify ps -q '*) "
            "service=${6}; [ \"${MOCK_DOWN:-}\" = \"$service\" ] || printf '%s\\n' \"$service\" ;;\n"
            "  'inspect --format '* ) printf '%s\\n' true ;;\n"
            "  'compose exec -T audio pactl '* ) "
            "case \"$*\" in *'list short sinks') printf '0\\torion\\tmodule-null-sink\\n' ;; "
            "*'list short modules') [ \"${MOCK_MISSING_DUCKING:-}\" = 1 ] || "
            "printf '1\\tmodule-role-ducking\\toptions\\n' ;; esac ;;\n"
            "esac\n"
        )
        docker.chmod(0o755)
        self.environment = os.environ.copy()
        self.environment.pop("ORION_UID", None)
        self.environment.pop("ORION_GID", None)
        self.environment["PATH"] = str(bindir) + os.pathsep + self.environment["PATH"]

    def run_script(self, name):
        return subprocess.run(
            ["sh", str(self.root / "scripts" / name)], cwd=self.root,
            env=self.environment, capture_output=True, text=True,
        )

    def test_setup_requires_configuration(self):
        result = self.run_script("setup-spotifyd.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Create .env", result.stderr)

    def test_setup_is_idempotent_and_preserves_cache(self):
        env_file = self.root / ".env"
        env_file.write_text("ORION_GATEWAY_URL=http://example.invalid")
        self.assertEqual(self.run_script("setup-spotifyd.sh").returncode, 0)
        cache = self.root / "spotifyd" / "cache"
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        (cache / "credentials.json").write_text("existing")
        configured = env_file.read_text()
        self.assertIn(f"\nORION_UID={os.getuid()}\n", configured)
        self.assertIn(f"\nORION_GID={os.getgid()}\n", configured)
        self.assertEqual(self.run_script("setup-spotifyd.sh").returncode, 0)
        self.assertEqual(env_file.read_text(), configured)
        self.assertEqual((cache / "credentials.json").read_text(), "existing")

    def test_setup_rejects_mismatched_identity(self):
        env_file = self.root / ".env"
        original = f"ORION_UID={os.getuid() + 1}\n"
        env_file.write_text(original)
        result = self.run_script("setup-spotifyd.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must match", result.stderr)
        self.assertEqual(env_file.read_text(), original)
        self.assertFalse((self.root / "spotifyd" / "cache").exists())

    def test_validator_detects_stopped_service(self):
        self.assertEqual(self.run_script("validate-spotifyd.sh").returncode, 0)
        self.environment["MOCK_DOWN"] = "spotifyd"
        result = self.run_script("validate-spotifyd.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("spotifyd is not running", result.stderr)

    def test_validator_detects_missing_ducking_module(self):
        self.environment["MOCK_MISSING_DUCKING"] = "1"
        result = self.run_script("validate-spotifyd.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ducking module is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()