"""Thin client for the telecom operator.

The interesting design decision lives here, not in the HTTP plumbing: what
we do when the operator call itself fails to give us a clean answer within
budget. There are exactly three outcomes a caller can act on:

  OperatorSuccess -- airtime was delivered. operator_ref is the vendor's
                     own reference for it.
  OperatorFailure -- the operator affirmatively told us nothing was
                     delivered (bad number, insufficient float on their
                     side, or a fast infrastructure error). Safe to
                     release the reserved funds back to the agent.
  OperatorUnknown -- we do not know. Either the HTTP call itself timed out
                     or the connection dropped before we got a response.
                     Crucially, this is NOT the same as "assume it failed"
                     -- the operator's own failure-injection includes a
                     case where the vend actually committed on its side
                     after our timeout fired. Guessing either way here is
                     exactly what the brief says not to do; the caller
                     must leave the transaction in NEEDS_RECONCILIATION.

This client deliberately does not retry or requery /operator/status itself.
A retry from inside the USSD request would spend more of the 8-second
gateway budget chasing an answer that isn't guaranteed to arrive; that
belongs to an out-of-band reconciliation pass instead (see README).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import requests
from django.conf import settings

logger = logging.getLogger("vending")


@dataclass(frozen=True)
class OperatorSuccess:
    operator_ref: str


@dataclass(frozen=True)
class OperatorFailure:
    reason: str


@dataclass(frozen=True)
class OperatorUnknown:
    reason: str


OperatorResult = OperatorSuccess | OperatorFailure | OperatorUnknown


class OperatorClient:
    def __init__(self, base_url: Optional[str] = None, timeout_seconds: Optional[float] = None):
        self.base_url = base_url or settings.OPERATOR_BASE_URL
        self.timeout_seconds = timeout_seconds or settings.OPERATOR_CALL_TIMEOUT_SECONDS

    def vend(self, *, reference: UUID, phone_number: str, amount_minor: int) -> OperatorResult:
        try:
            resp = requests.post(
                f"{self.base_url}/operator/vend",
                json={
                    "reference": str(reference),
                    "phoneNumber": phone_number,
                    "amountKobo": amount_minor,
                },
                timeout=self.timeout_seconds,
            )
        except requests.Timeout:
            logger.warning("operator.vend timeout reference=%s phone=%s", reference, phone_number)
            return OperatorUnknown(reason="OPERATOR_CALL_TIMEOUT")
        except requests.ConnectionError:
            logger.warning("operator.vend connection_error reference=%s phone=%s", reference, phone_number)
            return OperatorUnknown(reason="OPERATOR_CONNECTION_ERROR")

        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == "SUCCESS":
                return OperatorSuccess(operator_ref=body.get("operatorRef", ""))
            if body.get("status") == "FAILED":
                return OperatorFailure(reason=body.get("reason", "UNKNOWN_FAILURE"))
            logger.error("operator.vend unexpected 200 body reference=%s body=%s", reference, body)
            return OperatorUnknown(reason="UNEXPECTED_RESPONSE")

        if resp.status_code in (503, 429):
            # The operator affirmatively told us it did not attempt the
            # vend -- distinct from a timeout, and safe to treat as a clean
            # failure rather than an unknown.
            body = resp.json() if resp.content else {}
            reason = body.get("reason", "TRANSIENT" if resp.status_code == 503 else "RATE_LIMITED")
            return OperatorFailure(reason=reason)

        logger.error("operator.vend unexpected status reference=%s status=%s", reference, resp.status_code)
        return OperatorUnknown(reason=f"UNEXPECTED_STATUS_{resp.status_code}")

    def status(self, *, reference: UUID) -> OperatorResult:
        """Query the operator's own record of a reference. Not used on the
        request path -- this is the hook a reconciliation job would call
        for any transaction sitting in NEEDS_RECONCILIATION. Left here,
        implemented and tested, precisely so that job has something to call
        without needing to touch this module. See README "Reconciliation".
        """
        try:
            resp = requests.get(f"{self.base_url}/operator/status/{reference}", timeout=self.timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            return OperatorUnknown(reason=f"STATUS_CHECK_FAILED:{exc.__class__.__name__}")

        if resp.status_code != 200:
            return OperatorUnknown(reason=f"UNEXPECTED_STATUS_{resp.status_code}")

        body = resp.json()
        if body.get("status") == "SUCCESS":
            return OperatorSuccess(operator_ref=body.get("operatorRef", ""))
        if body.get("status") == "FAILED":
            return OperatorFailure(reason=body.get("reason", "UNKNOWN_FAILURE"))
        return OperatorUnknown(reason="STILL_PENDING")
