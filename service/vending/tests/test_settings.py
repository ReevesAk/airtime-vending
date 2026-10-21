"""Settings are evaluated once at import time, so each case imports
config.settings in a fresh interpreter with a controlled environment rather
than trying to reload the module inside the already-configured test process.
"""
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

SERVICE_DIR = Path(__file__).resolve().parents[2]

PRINT_KEY = "import config.settings as s; print(s.SECRET_KEY)"


def _import_settings(**env_overrides):
    env = {k: v for k, v in os.environ.items() if k not in ("DJANGO_SECRET_KEY", "DJANGO_DEBUG")}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", PRINT_KEY],
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


class SecretKeyTests(SimpleTestCase):
    def test_missing_key_without_debug_refuses_to_start(self):
        result = _import_settings(DJANGO_DEBUG="false")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_explicit_key_without_debug_is_used(self):
        result = _import_settings(DJANGO_DEBUG="false", DJANGO_SECRET_KEY="from-the-environment")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "from-the-environment")

    def test_missing_key_with_debug_falls_back_to_dev_key(self):
        result = _import_settings(DJANGO_DEBUG="true")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "dev-secret-key-not-for-production")
