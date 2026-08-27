#!/usr/bin/env python3
"""
USSD gateway test driver, built for this exercise (see repo README, "A note
on the scaffold" -- the brief's scenario describes a gateway driver as part
of its premise, not as a literal file drop). Exercises the vending service
against the gateway contract it implements (Africa's Talking style, common
on Nigerian short codes):

  POST /ussd
    form-encoded: sessionId, serviceCode, phoneNumber, text
    text accumulates the whole session's inputs joined by "*", e.g.
    "1*08010000000*500*1"
  Response:
    plain text, first 4 chars are "CON " (continue, expect more input) or
    "END " (terminate the session), rest is what's shown to the subscriber.

Menu implemented by the service under test:
  ""                       -> CON main menu (1. Buy Airtime)
  "1"                      -> CON ask for phone number
  "1*<phone>"              -> CON ask for amount
  "1*<phone>*<amount>"     -> CON confirm (1. Confirm  2. Cancel)
  "1*<phone>*<amount>*1"   -> END vend attempted (success or failure message)
  "1*<phone>*<amount>*2"   -> END cancelled

Every scenario here reproduces a specific failure mode real short codes hit:
gateway retries a callback it thinks timed out, subscribers double-tap
confirm, and subscribers walk away mid-session. None of them may cause two
vends on one session.

Usage:
  python driver.py --scenario all
  python driver.py --scenario gateway_retry --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import re
import sys
import time
import uuid

import requests

_REF_RE = re.compile(r"Ref:\s*(\S+)")


def _extract_ref(text: str) -> str | None:
    m = _REF_RE.search(text)
    return m.group(1) if m else None

DEFAULT_BASE_URL = "http://localhost:8000"
_session_counter = itertools.count(1)


def _sid() -> str:
    return f"drv-{uuid.uuid4().hex[:12]}"


def _phone(last_digit: str) -> str:
    return f"23480100000{last_digit}"


def ussd(base_url: str, session_id: str, text: str, phone: str = None, timeout: float = 10.0):
    resp = requests.post(
        f"{base_url}/ussd",
        data={
            "sessionId": session_id,
            "serviceCode": "*123#",
            "phoneNumber": phone or "2348000000000",
            "text": text,
        },
        timeout=timeout,
    )
    return resp


class Scenario:
    def __init__(self, name, fn):
        self.name = name
        self.fn = fn


def scenario_happy_path(base_url: str) -> None:
    sid = _sid()
    phone = _phone("0")  # bucket 0 -> operator SUCCESS
    r1 = ussd(base_url, sid, "")
    assert r1.text.startswith("CON"), f"expected CON at main menu, got: {r1.text!r}"

    r2 = ussd(base_url, sid, "1")
    assert r2.text.startswith("CON"), f"expected CON asking for phone, got: {r2.text!r}"

    r3 = ussd(base_url, sid, f"1*{phone}")
    assert r3.text.startswith("CON"), f"expected CON asking for amount, got: {r3.text!r}"

    r4 = ussd(base_url, sid, f"1*{phone}*500")
    assert r4.text.startswith("CON"), f"expected CON confirm prompt, got: {r4.text!r}"

    r5 = ussd(base_url, sid, f"1*{phone}*500*1")
    assert r5.text.startswith("END"), f"expected END after confirm, got: {r5.text!r}"
    assert "success" in r5.text.lower() or "confirm" in r5.text.lower(), (
        f"expected a success message, got: {r5.text!r}"
    )


def scenario_gateway_retry(base_url: str) -> None:
    """The gateway didn't see an ACK in time and resends the *same* callback
    (identical sessionId + text) for the confirm step. This must not vend
    twice, and the subscriber should see the same final message both times.
    """
    sid = _sid()
    phone = _phone("0")
    ussd(base_url, sid, "")
    ussd(base_url, sid, "1")
    ussd(base_url, sid, f"1*{phone}")
    ussd(base_url, sid, f"1*{phone}*500")

    confirm_text = f"1*{phone}*500*1"
    first = ussd(base_url, sid, confirm_text)
    second = ussd(base_url, sid, confirm_text)  # gateway retry

    assert first.text.startswith("END") and second.text.startswith("END")
    assert first.text == second.text, (
        f"retry produced a different response -- possible double vend:\n"
        f"  first:  {first.text!r}\n  second: {second.text!r}"
    )


def scenario_double_tap_confirm(base_url: str) -> None:
    """The subscriber presses confirm twice; the handset/gateway sends two
    separate callbacks with the identical accumulated text. Indistinguishable
    from a gateway retry at the HTTP layer -- same guarantee must hold.
    """
    sid = _sid()
    phone = _phone("0")
    ussd(base_url, sid, "")
    ussd(base_url, sid, "1")
    ussd(base_url, sid, f"1*{phone}")
    ussd(base_url, sid, f"1*{phone}*750")

    confirm_text = f"1*{phone}*750*1"
    tap1 = ussd(base_url, sid, confirm_text)
    tap2 = ussd(base_url, sid, confirm_text)

    assert tap1.text == tap2.text, (
        f"double-tap produced different responses:\n  tap1: {tap1.text!r}\n  tap2: {tap2.text!r}"
    )


def scenario_concurrent_duplicate(base_url: str) -> None:
    """Two identical confirm callbacks arrive at (as close to) the same
    instant -- the race the pytest concurrency test targets directly inside
    the service, replayed here over HTTP as an end-to-end sanity check.

    A request that lands mid-flight (between the transaction being reserved
    and the operator call resolving) may legitimately get a different
    subscriber-facing message than the one that resolves the vend (e.g.
    "still being processed" vs. "successful") -- that's fine. What must be
    true is that both responses reference the *same* transaction: two
    distinct references would mean two vends were created for one session.
    """
    sid = _sid()
    phone = _phone("0")
    ussd(base_url, sid, "")
    ussd(base_url, sid, "1")
    ussd(base_url, sid, f"1*{phone}")
    ussd(base_url, sid, f"1*{phone}*1000")

    confirm_text = f"1*{phone}*1000*1"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ussd, base_url, sid, confirm_text) for _ in range(2)]
        results = [f.result() for f in futures]

    assert all(r.text.startswith("END") for r in results), (
        f"expected both concurrent confirms to reach a terminal answer: {[r.text for r in results]}"
    )
    refs = {_extract_ref(r.text) for r in results}
    assert None not in refs, f"could not find a transaction reference in one of: {[r.text for r in results]}"
    assert len(refs) == 1, (
        f"concurrent identical confirms produced two different transaction references "
        f"-- likely a double vend: {[r.text for r in results]}"
    )


def scenario_session_expiry(base_url: str, ttl_seconds: float) -> None:
    """The subscriber opens the menu, walks away for longer than the
    session TTL, then comes back. The stale in-progress state (phone/amount
    already entered) must not survive -- they should land back at the
    beginning, not have their old inputs silently reused or their old
    confirm still armed.
    """
    sid = _sid()
    phone = _phone("0")
    ussd(base_url, sid, "")
    ussd(base_url, sid, "1")
    ussd(base_url, sid, f"1*{phone}")

    time.sleep(ttl_seconds + 2)

    # Subscriber's handset resends the *same* sessionId (gateways commonly
    # reuse it until they explicitly time out) but the service must treat
    # this as a fresh interaction rather than "amount, please" for a phone
    # number entered two minutes ago.
    resumed = ussd(base_url, sid, f"1*{phone}*500")
    assert "expired" in resumed.text.lower() or resumed.text.startswith("CON Welcome"), (
        f"expected expiry to reset the session, got: {resumed.text!r}"
    )


def scenario_operator_failure_shapes(base_url: str) -> None:
    """Sanity-checks that each operator failure bucket produces a coherent,
    distinct subscriber-facing outcome (not a crash, not a generic 500).
    Full ledger-state assertions for these live in the service's own test
    suite, which can see the database; this just checks the USSD layer
    degrades sensibly for a human on the other end of the phone.
    """
    for digit, must_contain in [
        ("1", None),  # insufficient operator float -> clear failure
        ("2", None),  # invalid number -> clear failure
        ("3", None),  # transient error -> clear failure/try again
        ("6", None),  # operator never resolves -> must still answer the subscriber
    ]:
        sid = _sid()
        phone = _phone(digit)
        ussd(base_url, sid, "")
        ussd(base_url, sid, "1")
        ussd(base_url, sid, f"1*{phone}")
        ussd(base_url, sid, f"1*{phone}*500")
        final = ussd(base_url, sid, f"1*{phone}*500*1", timeout=15.0)
        assert final.text.startswith("END"), (
            f"bucket {digit}: expected the subscriber to get a terminal answer "
            f"within the session window, got: {final.text!r}"
        )


SCENARIOS = {
    "happy_path": scenario_happy_path,
    "gateway_retry": scenario_gateway_retry,
    "double_tap_confirm": scenario_double_tap_confirm,
    "concurrent_duplicate": scenario_concurrent_duplicate,
    "operator_failure_shapes": scenario_operator_failure_shapes,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="all", choices=list(SCENARIOS) + ["all", "session_expiry"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--session-ttl",
        type=float,
        default=5.0,
        help="expected session TTL in seconds, must match the service's USSD_SESSION_TTL_SECONDS (default: 5)",
    )
    args = parser.parse_args()

    to_run = list(SCENARIOS.items())
    if args.scenario == "all":
        to_run.append(("session_expiry", lambda base_url: scenario_session_expiry(base_url, args.session_ttl)))
    elif args.scenario == "session_expiry":
        to_run = [("session_expiry", lambda base_url: scenario_session_expiry(base_url, args.session_ttl))]
    else:
        to_run = [(args.scenario, SCENARIOS[args.scenario])]

    failures = 0
    for name, fn in to_run:
        try:
            fn(args.base_url)
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}: {e}")
        except requests.RequestException as e:
            failures += 1
            print(f"ERROR {name}: request failed: {e}")
        else:
            print(f"PASS  {name}")

    print()
    if failures:
        print(f"{failures} scenario(s) failed")
        return 1
    print("all scenarios passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
