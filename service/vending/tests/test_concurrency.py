"""The test the brief says will be looked at first: concurrent identical
requests must never produce two vends for one session.

Uses TransactionTestCase (not TestCase) because Django's regular TestCase
wraps each test in a single outer transaction that isn't visible across
threads/connections -- it would hide exactly the race we're trying to
prove doesn't happen. TransactionTestCase gives each thread's connection a
real, independently-committed view of the database, which is what actually
exercises the SELECT ... FOR UPDATE guard in vending.ussd._claim_transaction.
"""
import threading
from unittest.mock import patch

from django.db import connections
from django.test import TransactionTestCase

from vending.models import Account, LedgerEntry, Transaction
from vending.operator_client import OperatorSuccess
from vending.ussd import DEMO_STARTING_FLOAT_MINOR, handle_ussd_request

CONFIRM_TEXT = "1*2348011112222*500*1"
SESSION_ID = "concurrent-sess-1"
AGENT_PHONE = "2348099990000"
CONCURRENT_REQUESTS = 12


class ConcurrentDuplicateVendTests(TransactionTestCase):
    @patch("vending.ussd.OperatorClient")
    def test_concurrent_identical_confirms_produce_exactly_one_vend(self, mock_operator_cls):
        mock_operator_cls.return_value.vend.return_value = OperatorSuccess(operator_ref="OP-RACE")

        barrier = threading.Barrier(CONCURRENT_REQUESTS)
        responses: list[str] = []
        lock = threading.Lock()

        def worker():
            barrier.wait()  # maximize actual overlap instead of a head start race
            try:
                result = handle_ussd_request(
                    session_id=SESSION_ID, agent_phone_number=AGENT_PHONE, text=CONFIRM_TEXT
                )
                with lock:
                    responses.append(result)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(CONCURRENT_REQUESTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(responses), CONCURRENT_REQUESTS, "every thread should get a response, none should hang")

        # The real assertion: exactly one Transaction and one balanced pair
        # of ledger entries exist for this session, no matter how many
        # identical confirms raced in.
        self.assertEqual(Transaction.objects.count(), 1)
        txn = Transaction.objects.get()
        self.assertEqual(txn.status, Transaction.SUCCESS)
        self.assertEqual(LedgerEntry.objects.filter(transaction=txn).count(), 2)

        agent = Account.objects.get(account_type=Account.AGENT_WALLET, agent_phone_number=AGENT_PHONE)
        self.assertEqual(
            agent.balance_minor, DEMO_STARTING_FLOAT_MINOR - 50_000,
            "agent must be charged exactly once regardless of how many duplicate confirms arrived",
        )

        # Every response must point at the same transaction -- proves none
        # of the racing requests silently created a second one.
        refs = {r.split("Ref: TXN")[-1] for r in responses}
        self.assertEqual(refs, {str(txn.pk)})

        # The operator must only ever have been called once. Calling it
        # again for a request that lost the race would mean either a
        # second real-money vend, or (best case) a wasted duplicate call --
        # both are bugs.
        self.assertEqual(mock_operator_cls.return_value.vend.call_count, 1)
