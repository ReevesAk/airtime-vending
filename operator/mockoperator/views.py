"""
A deterministic stand-in "telecom operator" service, built for this exercise
rather than integrated against a real vendor API (see repo README, "A note
on the scaffold" -- the brief's scenario describes an operator as part of
its premise, not as a literal file drop). It is deliberately built to
exhibit the failure taxonomy the brief calls for:

  - a clean success
  - clear, immediate failures (never touched the subscriber's line)
  - a transient error (operator confirms nothing happened, safe to retry)
  - a rate-limit style failure (retryable after backoff)
  - a slow-but-successful call (tests the 8s USSD budget without failing)
  - THE hard one: a call that hangs past the caller's timeout, but has
    actually committed on the operator's side by the time you ask again
  - a call that hangs and genuinely never happened
  - a call that hangs and never resolves at all (operator itself lost track)

Behavior is selected deterministically by the last digit of `phoneNumber`, so
scenarios are reproducible. It is NOT meant to be a faithful vendor API clone
-- real operator APIs are messier (inconsistent status codes, vendor-specific
error bodies) -- it exists to give the vending service something concrete
and realistic-shaped to be tested against.

Contract:

  POST /operator/vend
    body: {"reference": str, "phoneNumber": str, "amountKobo": int}
    -> 200 {"reference", "status": "SUCCESS"|"FAILED", "reason"?, "operatorRef"?}
    -> 503 {"reference", "status": "ERROR", "reason": "TRANSIENT"}   (nothing committed)
    -> 429 {"reference", "status": "ERROR", "reason": "RATE_LIMITED"} (nothing committed)
    -> hangs (no response) for the TIMEOUT_* buckets -- caller times out client-side

  GET /operator/status/<reference>
    -> 200 {"reference", "status": "SUCCESS"|"FAILED"|"PENDING", "reason"?, "operatorRef"?}

The /status endpoint is the operator's own system-of-record lookup -- the
resolution path for anything that timed out on the vend call itself.

All state lives in the in-memory `_records` dict below, guarded by a plain
lock -- there is deliberately no database here (see config/settings.py).
Concurrent requests are handled because Django's `runserver` is threaded by
default; that's what lets the TIMEOUT_* buckets sleep in one request's
thread while other requests (and the background resolver thread started for
that reference) keep being served.
"""
from __future__ import annotations

import json
import os
import random
import string
import threading
import time

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

_lock = threading.Lock()
_records: dict[str, dict] = {}  # reference -> {"status": ..., "reason": ..., "operatorRef": ...}

# How long the harness sleeps for the three TIMEOUT_* buckets before the
# in-memory record becomes visible via /status. Comfortably past any sane
# client-side timeout (we tell candidates to budget ~4-5s for the operator
# call inside an 8s USSD window). Overridable so tests can use a much
# shorter delay instead of waiting out the production-realistic default.
COMMIT_DELAY_SECONDS = float(os.environ.get("OPERATOR_COMMIT_DELAY_SECONDS", "6.0"))

# The "slow but fine" bucket: succeeds, but only after this many seconds.
SLOW_SUCCESS_SECONDS = float(os.environ.get("OPERATOR_SLOW_SUCCESS_SECONDS", "3.0"))


def _gen_operator_ref() -> str:
    return "OP-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


def _bucket(phone_number: str) -> str:
    last = phone_number.strip()[-1:] or "0"
    return {
        "0": "SUCCESS",
        "1": "INSUFFICIENT_FUNDS",
        "2": "INVALID_NUMBER",
        "3": "TRANSIENT_ERROR",
        "4": "TIMEOUT_THEN_COMMITTED",
        "5": "TIMEOUT_THEN_FAILED",
        "6": "TIMEOUT_THEN_UNKNOWN",
        "7": "SUCCESS",
        "8": "SLOW_SUCCESS",
        "9": "RATE_LIMITED",
    }.get(last, "SUCCESS")


@csrf_exempt
@require_POST
def vend(request):
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {}

    reference = payload.get("reference")
    phone_number = payload.get("phoneNumber", "")
    amount_kobo = payload.get("amountKobo")

    if not reference or not phone_number or not amount_kobo:
        return JsonResponse({"status": "ERROR", "reason": "BAD_REQUEST"}, status=400)

    with _lock:
        existing = _records.get(reference)
    if existing is not None:
        # Real operators are commonly idempotent on a client-supplied
        # reference. A caller that retries with the same reference after a
        # network hiccup gets back whatever was already decided -- it does
        # not trigger a second vend.
        return JsonResponse({"reference": reference, **existing})

    bucket = _bucket(phone_number)

    if bucket == "SUCCESS":
        record = {"status": "SUCCESS", "operatorRef": _gen_operator_ref()}
        with _lock:
            _records[reference] = record
        return JsonResponse({"reference": reference, **record})

    if bucket == "INSUFFICIENT_FUNDS":
        record = {"status": "FAILED", "reason": "INSUFFICIENT_OPERATOR_FLOAT"}
        with _lock:
            _records[reference] = record
        return JsonResponse({"reference": reference, **record})

    if bucket == "INVALID_NUMBER":
        record = {"status": "FAILED", "reason": "INVALID_NUMBER"}
        with _lock:
            _records[reference] = record
        return JsonResponse({"reference": reference, **record})

    if bucket == "TRANSIENT_ERROR":
        # Deliberately NOT recorded -- the operator confirms nothing
        # happened, so a retry with a *new* reference is safe.
        return JsonResponse({"reference": reference, "status": "ERROR", "reason": "TRANSIENT"}, status=503)

    if bucket == "RATE_LIMITED":
        return JsonResponse({"reference": reference, "status": "ERROR", "reason": "RATE_LIMITED"}, status=429)

    if bucket == "SLOW_SUCCESS":
        time.sleep(SLOW_SUCCESS_SECONDS)
        record = {"status": "SUCCESS", "operatorRef": _gen_operator_ref()}
        with _lock:
            _records[reference] = record
        return JsonResponse({"reference": reference, **record})

    if bucket in ("TIMEOUT_THEN_COMMITTED", "TIMEOUT_THEN_FAILED"):
        final_status = "SUCCESS" if bucket == "TIMEOUT_THEN_COMMITTED" else "FAILED"
        final = {"status": final_status}
        if final_status == "SUCCESS":
            final["operatorRef"] = _gen_operator_ref()
        else:
            final["reason"] = "DELIVERY_FAILED_DOWNSTREAM"

        def _resolve_later():
            time.sleep(COMMIT_DELAY_SECONDS)
            with _lock:
                _records[reference] = final

        threading.Thread(target=_resolve_later, daemon=True).start()
        # Sleep well past the caller's expected timeout so the HTTP call
        # itself never returns a useful answer -- the caller must give up
        # and later ask /status.
        time.sleep(COMMIT_DELAY_SECONDS + 5)
        return JsonResponse({"reference": reference, "status": "ERROR", "reason": "NO_RESPONSE"}, status=504)

    if bucket == "TIMEOUT_THEN_UNKNOWN":
        # Never resolves. Simulates the operator itself losing track of the
        # request -- there is genuinely no answer to fetch later. This is
        # the case a resolution mechanism can only escalate, not solve
        # automatically (see README: settlement-file reconciliation).
        time.sleep(COMMIT_DELAY_SECONDS + 5)
        return JsonResponse({"reference": reference, "status": "ERROR", "reason": "NO_RESPONSE"}, status=504)

    record = {"status": "SUCCESS", "operatorRef": _gen_operator_ref()}
    with _lock:
        _records[reference] = record
    return JsonResponse({"reference": reference, **record})


@require_GET
def status(request, reference: str):
    with _lock:
        record = _records.get(reference)
    if record is None:
        return JsonResponse({"reference": reference, "status": "PENDING"})
    return JsonResponse({"reference": reference, **record})


@require_GET
def health(request):
    return JsonResponse({"ok": True})
