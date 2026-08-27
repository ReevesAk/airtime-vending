"""The USSD session state machine and vend orchestration.

Menu (Africa's Talking-style accumulating `text`):
  ""                      -> main menu
  "1"                     -> ask for customer phone number
  "1*<phone>"             -> ask for amount
  "1*<phone>*<amount>"    -> confirm prompt
  "1*<phone>*<amount>*1"  -> attempt the vend, terminal response
  "1*<phone>*<amount>*2"  -> cancelled, terminal response

Deliberately stateless about *menu position*: every callback carries the
full accumulated text, so the current step is derived by parsing it rather
than trusted from a stored "current step" field. That means a retried or
duplicated callback for any of the first three steps is naturally
idempotent -- it just re-derives and re-sends the same prompt. The one step
that is NOT naturally idempotent is the final confirm, because it is the
only one that does something (moves money, calls the operator) -- that one
is guarded explicitly in _claim_transaction.
"""
from __future__ import annotations

import logging
from uuid import uuid4

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Account, LedgerEntry, Transaction, UssdSession
from .operator_client import OperatorClient, OperatorFailure, OperatorSuccess, OperatorUnknown

logger = logging.getLogger("vending")

# Onboarding a real agent (KYC, funding their float via a real payment rail)
# is out of scope for this exercise. Any agent phone number that dials in
# and doesn't already have a wallet gets one auto-provisioned with this
# starting float so the flow is exercisable end to end.
DEMO_STARTING_FLOAT_MINOR = 5_000_000  # NGN 50,000.00

MAX_VEND_AMOUNT_NAIRA = 50_000

_FAILURE_MESSAGES = {
    "INSUFFICIENT_OPERATOR_FLOAT": "the operator could not process it right now",
    "INVALID_NUMBER": "that phone number is not valid",
    "TRANSIENT": "the operator is temporarily unavailable, please try again shortly",
    "RATE_LIMITED": "the operator is busy, please try again shortly",
}


def handle_ussd_request(session_id: str, agent_phone_number: str, text: str) -> str:
    now = timezone.now()
    session, created = UssdSession.objects.get_or_create(
        session_id=session_id, defaults={"agent_phone_number": agent_phone_number}
    )

    if not created:
        idle_for = (now - session.last_seen_at).total_seconds()
        if idle_for > settings.USSD_SESSION_TTL_SECONDS and text != "":
            session.last_seen_at = now
            session.save(update_fields=["last_seen_at"])
            logger.info("ussd.session_expired session_id=%s idle_for=%.1fs", session_id, idle_for)
            return "END Your session has expired. Please dial in again."

    session.last_seen_at = now
    session.save(update_fields=["last_seen_at"])

    parts = [p for p in text.split("*") if p != ""] if text else []

    if not parts:
        return "CON Welcome to Airtime Vending\n1. Buy airtime"

    if parts[0] != "1":
        return "END Invalid option."

    if len(parts) == 1:
        return "CON Enter the customer's phone number"

    if len(parts) == 2:
        phone = parts[1]
        if not _looks_like_phone(phone):
            return "CON Invalid phone number. Enter the customer's phone number"
        return "CON Enter amount (NGN)"

    if len(parts) == 3:
        phone, amount_str = parts[1], parts[2]
        if not _looks_like_phone(phone):
            return "CON Invalid phone number. Enter the customer's phone number"
        if not _looks_like_amount(amount_str):
            return f"CON Invalid amount (max NGN{MAX_VEND_AMOUNT_NAIRA}). Enter amount (NGN)"
        return f"CON Top up {phone} with NGN{amount_str}?\n1. Confirm\n2. Cancel"

    if len(parts) == 4:
        phone, amount_str, choice = parts[1], parts[2], parts[3]
        if not _looks_like_phone(phone) or not _looks_like_amount(amount_str):
            return "END Something went wrong with your entry. Please dial in again."
        if choice == "2":
            return "END Top-up cancelled. No funds were charged."
        if choice != "1":
            return "END Invalid option."
        return _confirm_vend(session, agent_phone_number, phone, int(amount_str))

    return "END Invalid input. Please dial in again."


def _looks_like_phone(value: str) -> bool:
    return value.isdigit() and 10 <= len(value) <= 14


def _looks_like_amount(value: str) -> bool:
    return value.isdigit() and 0 < int(value) <= MAX_VEND_AMOUNT_NAIRA


def _get_or_create_agent_account(phone_number: str) -> Account:
    account, created = Account.objects.get_or_create(
        account_type=Account.AGENT_WALLET,
        agent_phone_number=phone_number,
        defaults={"balance_minor": DEMO_STARTING_FLOAT_MINOR},
    )
    if created:
        logger.info("agent.wallet_provisioned phone=%s starting_float_minor=%s", phone_number, DEMO_STARTING_FLOAT_MINOR)
    return account


def _get_clearing_account() -> Account:
    account, _ = Account.objects.get_or_create(
        account_type=Account.OPERATOR_CLEARING,
        agent_phone_number=None,
        defaults={"balance_minor": 0},
    )
    return account


def _claim_transaction(session: UssdSession, agent_phone_number: str, customer_phone: str, amount_minor: int):
    """Atomically ensure at most one Transaction is ever attached to this
    session. Returns (transaction_or_none, created).

    `created=False` with a transaction means this is a replay (gateway
    retry or subscriber double-tap) of a confirm that was already claimed
    -- the caller must NOT call the operator again, just report the
    existing outcome. `transaction=None` means the vend was rejected before
    any funds moved (insufficient balance) and nothing was recorded.

    Lock ordering is always session -> agent -> clearing, consistently,
    across every code path that touches more than one of these rows
    (see _reverse_reservation), so two concurrent vends can never deadlock
    against each other.
    """
    with db_transaction.atomic():
        locked_session = UssdSession.objects.select_for_update().get(pk=session.pk)
        if locked_session.transaction_id is not None:
            return locked_session.transaction, False

        agent = _get_or_create_agent_account(agent_phone_number)
        agent = Account.objects.select_for_update().get(pk=agent.pk)
        if agent.balance_minor < amount_minor:
            logger.info(
                "vend.rejected_insufficient_funds session_id=%s agent=%s amount_minor=%s balance_minor=%s",
                session.session_id, agent_phone_number, amount_minor, agent.balance_minor,
            )
            return None, False

        clearing = _get_clearing_account()
        clearing = Account.objects.select_for_update().get(pk=clearing.pk)

        txn = Transaction.objects.create(
            operator_reference=uuid4(),
            agent=agent,
            customer_phone_number=customer_phone,
            amount_minor=amount_minor,
            status=Transaction.PENDING,
        )
        LedgerEntry.objects.create(transaction=txn, account=agent, entry_type=LedgerEntry.DEBIT, amount_minor=amount_minor)
        LedgerEntry.objects.create(transaction=txn, account=clearing, entry_type=LedgerEntry.CREDIT, amount_minor=amount_minor)
        agent.balance_minor -= amount_minor
        agent.save(update_fields=["balance_minor"])
        clearing.balance_minor += amount_minor
        clearing.save(update_fields=["balance_minor"])

        locked_session.transaction = txn
        locked_session.save(update_fields=["transaction"])

        logger.info(
            "vend.reserved txn=%s session_id=%s agent=%s customer=%s amount_minor=%s",
            txn.pk, session.session_id, agent_phone_number, customer_phone, amount_minor,
        )
        return txn, True


def _reverse_reservation(txn: Transaction) -> None:
    agent = Account.objects.select_for_update().get(pk=txn.agent_id)
    clearing = _get_clearing_account()
    clearing = Account.objects.select_for_update().get(pk=clearing.pk)

    LedgerEntry.objects.create(transaction=txn, account=agent, entry_type=LedgerEntry.CREDIT, amount_minor=txn.amount_minor)
    LedgerEntry.objects.create(transaction=txn, account=clearing, entry_type=LedgerEntry.DEBIT, amount_minor=txn.amount_minor)
    agent.balance_minor += txn.amount_minor
    agent.save(update_fields=["balance_minor"])
    clearing.balance_minor -= txn.amount_minor
    clearing.save(update_fields=["balance_minor"])


def _finalize(txn: Transaction) -> None:
    outcome = OperatorClient().vend(
        reference=txn.operator_reference, phone_number=txn.customer_phone_number, amount_minor=txn.amount_minor
    )
    with db_transaction.atomic():
        locked_txn = Transaction.objects.select_for_update().get(pk=txn.pk)
        if isinstance(outcome, OperatorSuccess):
            locked_txn.status = Transaction.SUCCESS
            locked_txn.operator_ref = outcome.operator_ref
            locked_txn.save(update_fields=["status", "operator_ref", "updated_at"])
            logger.info("vend.success txn=%s operator_ref=%s", locked_txn.pk, outcome.operator_ref)
        elif isinstance(outcome, OperatorFailure):
            _reverse_reservation(locked_txn)
            locked_txn.status = Transaction.FAILED
            locked_txn.failure_reason = outcome.reason
            locked_txn.save(update_fields=["status", "failure_reason", "updated_at"])
            logger.info("vend.failed txn=%s reason=%s", locked_txn.pk, outcome.reason)
        elif isinstance(outcome, OperatorUnknown):
            locked_txn.status = Transaction.NEEDS_RECONCILIATION
            locked_txn.failure_reason = outcome.reason
            locked_txn.save(update_fields=["status", "failure_reason", "updated_at"])
            logger.warning("vend.needs_reconciliation txn=%s reason=%s", locked_txn.pk, outcome.reason)


def _human_reason(reason: str | None) -> str:
    if reason is None:
        return "an unexpected error"
    return _FAILURE_MESSAGES.get(reason, reason)


def _render_outcome(txn: Transaction) -> str:
    ref = f"Ref: TXN{txn.pk}"
    if txn.status == Transaction.SUCCESS:
        return f"END Top-up of NGN{txn.amount_minor // 100} to {txn.customer_phone_number} was successful. {ref}"
    if txn.status == Transaction.FAILED:
        return f"END Top-up failed: {_human_reason(txn.failure_reason)}. Your balance was not charged. {ref}"
    if txn.status == Transaction.NEEDS_RECONCILIATION:
        return f"END We could not confirm your top-up. If it does not reflect shortly, contact support. {ref}"
    return f"END Your top-up request is still being processed. You will be notified shortly. {ref}"


def _confirm_vend(session: UssdSession, agent_phone_number: str, customer_phone: str, amount_naira: int) -> str:
    amount_minor = amount_naira * 100
    txn, created = _claim_transaction(session, agent_phone_number, customer_phone, amount_minor)

    if txn is None:
        return "END Insufficient balance. Please recharge your float."

    if not created:
        logger.info("vend.replay txn=%s session_id=%s status=%s", txn.pk, session.session_id, txn.status)
        return _render_outcome(txn)

    _finalize(txn)
    txn.refresh_from_db()
    return _render_outcome(txn)
