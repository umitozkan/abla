"""First-run configuration preserves secrets and refuses existing paths."""

from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_env.py"


class SetupEnvTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="emine-setup-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_setup(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            cwd=self.directory,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def read_settings(self, path):
        return dict(
            line.split("=", 1)
            for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        )

    def test_production_settings_and_private_file(self):
        result = self.run_setup("--production")
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.directory / ".env"
        settings = self.read_settings(path)
        expected = {
            "COMPOSE_PROJECT_NAME": "abla",
            "APP_ENV": "production",
            "APP_PORT": "18082",
            "COOKIE_SECURE": "true",
            "ALLOWED_HOSTS": "abla.umitozkan.com.tr,localhost,127.0.0.1",
            "ADMIN_USERNAME": "yonetici",
            "EMINE_USERNAME": "emine",
        }
        for key, value in expected.items():
            self.assertEqual(settings[key], value)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_secrets_are_random_distinct_and_not_printed(self):
        results = []
        generated = []
        for name in ("first.env", "second.env"):
            result = self.run_setup("--production", "--output", name)
            self.assertEqual(result.returncode, 0, result.stderr)
            results.append(result.stdout + result.stderr)
            settings = self.read_settings(self.directory / name)
            self.assertRegex(settings["SECRET_KEY"], r"^[0-9a-f]{64}$")
            for key in ("SECRET_KEY", "ADMIN_PASSWORD", "EMINE_PASSWORD"):
                secret = settings[key]
                self.assertGreaterEqual(len(secret), 32)
                self.assertRegex(secret, r"^[A-Za-z0-9_-]+$")
                generated.append(secret)
        self.assertEqual(len(set(generated)), 6)
        for secret in generated:
            self.assertNotIn(secret, "".join(results))

    def test_development_keeps_local_port(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = self.read_settings(self.directory / ".env")
        self.assertEqual(settings["APP_ENV"], "development")
        self.assertEqual(settings["APP_PORT"], "8080")
        self.assertEqual(settings["COOKIE_SECURE"], "false")
        self.assertEqual(settings["COMPOSE_PROJECT_NAME"], "emine")

    def test_existing_file_is_not_overwritten_or_chmodded(self):
        target = self.directory / ".env"
        original = b"SECRET_KEY=existing-sensitive-value\n"
        target.write_bytes(original)
        target.chmod(0o640)
        result = self.run_setup("--production")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertNotIn("existing-sensitive-value", result.stdout + result.stderr)

    def test_existing_symlink_target_is_not_modified(self):
        target = self.directory / "existing.env"
        target.write_text("PRESERVE=original\n")
        link = self.directory / ".env"
        link.symlink_to(target)
        result = self.run_setup("--production")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "PRESERVE=original\n")

    def test_dangling_symlink_is_not_followed(self):
        target = self.directory / "nonexistent.env"
        link = self.directory / ".env"
        link.symlink_to(target)
        result = self.run_setup("--production")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(link.is_symlink())
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
