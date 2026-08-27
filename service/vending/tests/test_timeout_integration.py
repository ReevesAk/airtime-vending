"""End-to-end test against the *real* mock operator process (not mocked
HTTP) for the one failure shape the brief calls out specifically: a vend
call that times out on our side after it has already committed on the
operator's. This is deliberately not a unit test with a stubbed response --
the whole point is proving the client's behavior against the same Django
project that will actually run in docker-compose, not a story we tell
ourselves in a mock.

Spawns operator/manage.py as a subprocess. Skipped automatically if that
file can't be found (e.g. this module got copied out of the repo on its
own), so the rest of the suite still runs standalone.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from django.test import SimpleTestCase

from vending.operator_client import OperatorClient, OperatorSuccess, OperatorUnknown

OPERATOR_MANAGE_PY = Path(__file__).resolve().parents[3] / "operator" / "manage.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, timeout: float = 5.0) -> None:
    import requests

    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            requests.get(f"{base_url}/operator/health", timeout=0.5)
            return
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"operator did not become healthy: {last_error}")


class TimeoutThenCommittedIntegrationTests(SimpleTestCase):
    """SimpleTestCase: this doesn't touch Django's database at all, it's
    purely exercising OperatorClient against a live process."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not OPERATOR_MANAGE_PY.exists():
            return
        cls.port = _free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = {**os.environ, "OPERATOR_COMMIT_DELAY_SECONDS": "1.0"}
        cls.proc = subprocess.Popen(
            [sys.executable, str(OPERATOR_MANAGE_PY), "runserver", f"127.0.0.1:{cls.port}", "--noreload"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_health(cls.base_url, timeout=8.0)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "proc", None) is not None:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        super().tearDownClass()

    def setUp(self):
        if not OPERATOR_MANAGE_PY.exists():
            self.skipTest(f"operator/manage.py not found at {OPERATOR_MANAGE_PY}")

    def test_timeout_then_committed_is_reported_as_unknown_not_success(self):
        # Client timeout (0.3s) fires well before the operator's 1.0s
        # commit delay -- this is the exact race the brief warns about.
        client = OperatorClient(base_url=self.base_url, timeout_seconds=0.3)
        reference = "test-timeout-committed-ref"

        result = client.vend(reference=reference, phone_number="2348000000004", amount_minor=50000)

        self.assertIsInstance(
            result, OperatorUnknown,
            "a client-side timeout must never be reported as success or failure -- "
            "the vend may have already committed on the operator's side",
        )

        # The operator *did* commit -- prove it by waiting past its resolve
        # delay and querying /operator/status directly, which is exactly
        # the call a reconciliation job would make later.
        time.sleep(1.5)
        resolved = client.status(reference=reference)
        self.assertIsInstance(resolved, OperatorSuccess)
        self.assertTrue(resolved.operator_ref)

    def test_timeout_then_failed_resolves_to_failure_on_status_check(self):
        client = OperatorClient(base_url=self.base_url, timeout_seconds=0.3)
        reference = "test-timeout-failed-ref"

        result = client.vend(reference=reference, phone_number="2348000000005", amount_minor=50000)
        self.assertIsInstance(result, OperatorUnknown)

        time.sleep(1.5)
        resolved = client.status(reference=reference)
        self.assertEqual(resolved.reason, "DELIVERY_FAILED_DOWNSTREAM")
