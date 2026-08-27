"""Ledger correctness for each of the three terminal-ish outcomes a vend
can reach. The operator call itself is mocked here -- this is testing our
bookkeeping given a known operator response, not the operator's behavior
(see test_operator_client.py and test_timeout_integration.py for that).
"""
from unittest.mock import patch

from django.test import TestCase

from vending.models import Account, LedgerEntry, Transaction
from vending.operator_client import OperatorFailure, OperatorSuccess, OperatorUnknown
from vending.ussd import DEMO_STARTING_FLOAT_MINOR, handle_ussd_request

CONFIRM_TEXT = "1*2348011112222*500*1"  # NGN 500 -> 50,000 minor units


def _confirm(session_id="sess-1", agent_phone="2348099990000", text=CONFIRM_TEXT):
    return handle_ussd_request(session_id=session_id, agent_phone_number=agent_phone, text=text)


def _assert_globally_balanced(test_case):
    total_debits = sum(LedgerEntry.objects.filter(entry_type=LedgerEntry.DEBIT).values_list("amount_minor", flat=True))
    total_credits = sum(LedgerEntry.objects.filter(entry_type=LedgerEntry.CREDIT).values_list("amount_minor", flat=True))
    test_case.assertEqual(total_debits, total_credits)


class SuccessfulVendLedgerTests(TestCase):
    @patch("vending.ussd.OperatorClient")
    def test_success_posts_balanced_entries_and_charges_agent(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-123")

        response = _confirm()

        self.assertIn("successful", response)
        txn = Transaction.objects.get()
        self.assertEqual(txn.status, Transaction.SUCCESS)
        self.assertEqual(txn.operator_ref, "OP-123")

        entries = list(LedgerEntry.objects.filter(transaction=txn))
        self.assertEqual(len(entries), 2)
        self.assertEqual({e.entry_type for e in entries}, {LedgerEntry.DEBIT, LedgerEntry.CREDIT})
        for e in entries:
            self.assertEqual(e.amount_minor, 50_000)

        agent = Account.objects.get(account_type=Account.AGENT_WALLET)
        clearing = Account.objects.get(account_type=Account.OPERATOR_CLEARING)
        self.assertEqual(agent.balance_minor, DEMO_STARTING_FLOAT_MINOR - 50_000)
        self.assertEqual(clearing.balance_minor, 50_000)
        _assert_globally_balanced(self)


class FailedVendLedgerTests(TestCase):
    @patch("vending.ussd.OperatorClient")
    def test_clean_failure_reverses_the_reservation(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorFailure(reason="INVALID_NUMBER")

        response = _confirm()

        self.assertIn("failed", response.lower())
        txn = Transaction.objects.get()
        self.assertEqual(txn.status, Transaction.FAILED)
        self.assertEqual(txn.failure_reason, "INVALID_NUMBER")

        entries = list(LedgerEntry.objects.filter(transaction=txn).order_by("id"))
        self.assertEqual(len(entries), 4, "expected the original reservation pair plus a reversal pair")

        agent = Account.objects.get(account_type=Account.AGENT_WALLET)
        clearing = Account.objects.get(account_type=Account.OPERATOR_CLEARING)
        self.assertEqual(agent.balance_minor, DEMO_STARTING_FLOAT_MINOR, "failed vend must not leave the agent charged")
        self.assertEqual(clearing.balance_minor, 0)
        _assert_globally_balanced(self)


class NeedsReconciliationLedgerTests(TestCase):
    @patch("vending.ussd.OperatorClient")
    def test_unknown_outcome_leaves_funds_reserved_pending_resolution(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorUnknown(reason="OPERATOR_CALL_TIMEOUT")

        response = _confirm()

        self.assertIn("could not confirm", response.lower())
        txn = Transaction.objects.get()
        self.assertEqual(txn.status, Transaction.NEEDS_RECONCILIATION)
        self.assertEqual(txn.failure_reason, "OPERATOR_CALL_TIMEOUT")

        # Same ledger shape as a success -- the ambiguity lives in
        # Transaction.status, not in the entries themselves. This is the
        # "distinguishable in the data, not inferred from timestamps" bit:
        # a NEEDS_RECONCILIATION and a SUCCESS transaction have identical
        # ledger footprints and are told apart purely by `status`.
        entries = list(LedgerEntry.objects.filter(transaction=txn))
        self.assertEqual(len(entries), 2)

        agent = Account.objects.get(account_type=Account.AGENT_WALLET)
        self.assertEqual(agent.balance_minor, DEMO_STARTING_FLOAT_MINOR - 50_000)
        _assert_globally_balanced(self)


class InsufficientBalanceTests(TestCase):
    def test_insufficient_balance_never_touches_the_ledger(self):
        agent = Account.objects.create(
            account_type=Account.AGENT_WALLET, agent_phone_number="2348099990000", balance_minor=100
        )

        response = _confirm()

        self.assertIn("Insufficient balance", response)
        self.assertEqual(Transaction.objects.count(), 0)
        self.assertEqual(LedgerEntry.objects.count(), 0)
        agent.refresh_from_db()
        self.assertEqual(agent.balance_minor, 100)


class DoubleEntrySchemaTests(TestCase):
    """The brief asks that the schema itself make debits=credits legible,
    not just an accident of application logic that happens to hold today.
    """

    @patch("vending.ussd.OperatorClient")
    def test_mixed_outcomes_stay_globally_balanced(self, mock_operator_cls):
        outcomes = [
            OperatorSuccess(operator_ref="OP-1"),
            OperatorFailure(reason="INVALID_NUMBER"),
            OperatorUnknown(reason="OPERATOR_CALL_TIMEOUT"),
        ]
        for i, outcome in enumerate(outcomes):
            mock_operator_cls.return_value.vend.return_value = outcome
            _confirm(session_id=f"sess-{i}", text=f"1*234801111222{i}*{100 + i}*1")

        self.assertEqual(Transaction.objects.count(), 3)
        _assert_globally_balanced(self)

    def test_every_transaction_has_a_debit_and_a_credit_leg_of_equal_amount(self):
        with patch("vending.ussd.OperatorClient") as mock_operator_cls:
            mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-1")
            _confirm()

        for txn in Transaction.objects.all():
            entries = list(LedgerEntry.objects.filter(transaction=txn))
            debit_total = sum(e.amount_minor for e in entries if e.entry_type == LedgerEntry.DEBIT)
            credit_total = sum(e.amount_minor for e in entries if e.entry_type == LedgerEntry.CREDIT)
            self.assertEqual(debit_total, credit_total)
