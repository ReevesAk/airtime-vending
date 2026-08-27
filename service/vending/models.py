from django.db import models


class Account(models.Model):
    """A ledger account. Two kinds exist in this scope:

    AGENT_WALLET       -- one per field agent, holds their prepaid float as
                           a liability (the platform owes them this much
                           value). Identified by the phone number they dial
                           the short code from. Credited when they fund
                           their float (out of scope -- seeded directly);
                           debited when they spend it on a vend.
    OPERATOR_CLEARING  -- a single account, also a liability: what the
                           platform owes the operator for airtime already
                           delivered but not yet paid for on the operator's
                           daily settlement cycle (see README
                           "Reconciliation"). Credited when a vend is
                           reserved; debited back if that vend fails.

    `balance_minor` is a maintained cache, not the source of truth -- the
    source of truth is the sum of this account's LedgerEntry rows. It
    exists purely so a vend can take a row lock (SELECT ... FOR UPDATE) on
    a single small row instead of aggregating the whole ledger under lock
    on every request. See tests/test_ledger.py for the invariant check that
    the cache always agrees with the sum of entries.
    """

    AGENT_WALLET = "AGENT_WALLET"
    OPERATOR_CLEARING = "OPERATOR_CLEARING"
    ACCOUNT_TYPES = [
        (AGENT_WALLET, "Agent wallet"),
        (OPERATOR_CLEARING, "Operator clearing"),
    ]

    account_type = models.CharField(max_length=32, choices=ACCOUNT_TYPES)
    # Only meaningful for AGENT_WALLET accounts; null for the single
    # OPERATOR_CLEARING account.
    agent_phone_number = models.CharField(max_length=20, null=True, blank=True, unique=True)
    balance_minor = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(account_type="OPERATOR_CLEARING") | models.Q(agent_phone_number__isnull=False),
                name="agent_wallet_requires_phone_number",
            ),
        ]

    def __str__(self):
        return f"{self.account_type}:{self.agent_phone_number or 'clearing'}"


class UssdSession(models.Model):
    """One row per USSD session. Deliberately holds almost no menu state --
    the gateway resends the full accumulated `text` on every callback, so
    the current menu step is derived by parsing it (see vending/ussd.py).

    What this row *does* need to hold:
      - `last_seen_at`, to detect a subscriber who walked away and expire
        their session rather than resume stale state.
      - `transaction`, a one-to-one link that is the actual duplicate-vend
        guard: the OneToOneField below puts a unique constraint on
        transaction_id at the database level, so even two requests that
        both race past an application-level check cannot both attach a
        transaction to the same session -- the second insert fails and is
        handled as "already vended, return the existing outcome."
    """

    session_id = models.CharField(max_length=128, primary_key=True)
    agent_phone_number = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    transaction = models.OneToOneField(
        "Transaction", null=True, blank=True, on_delete=models.PROTECT, related_name="session"
    )

    def __str__(self):
        return self.session_id


class Transaction(models.Model):
    """One attempted vend. Status is the single source of truth for where
    the money is -- never inferred from timestamps or ledger row counts.

    PENDING                -- funds have been moved from the agent's wallet
                               to the clearing account and the operator has
                               been (or is about to be) called. Not yet
                               known whether airtime was delivered.
    SUCCESS                -- operator confirmed delivery. Ledger entries
                               from the PENDING step stand as final.
    FAILED                 -- operator confirmed nothing was delivered (or
                               we never attempted the call, e.g. bad
                               request). A reversing ledger entry pair is
                               posted, returning the funds to the agent.
    NEEDS_RECONCILIATION   -- the operator call timed out or the connection
                               dropped *after* we cannot be sure whether it
                               committed. Funds stay exactly where the
                               PENDING step put them -- neither confirmed
                               delivered nor returned -- until an
                               out-of-band check (operator status query or
                               daily settlement file) resolves it. This is
                               the terminal-but-unresolved state the brief
                               asks for: it has an explicit, described path
                               to resolution (README "Reconciliation"),
                               it just isn't automated in this exercise.
    """

    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (SUCCESS, "Success"),
        (FAILED, "Failed"),
        (NEEDS_RECONCILIATION, "Needs reconciliation"),
    ]

    # Sent to the operator as the idempotency key on /operator/vend, and
    # used to look up the operator's own /operator/status/<reference> in a
    # future reconciliation pass. A UUID, not the DB primary key, so it can
    # be generated before the row is committed if ever needed.
    operator_reference = models.UUIDField(unique=True)
    operator_ref = models.CharField(max_length=64, null=True, blank=True)  # the operator's own opaque ref

    agent = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="transactions")
    customer_phone_number = models.CharField(max_length=20)
    amount_minor = models.PositiveBigIntegerField()

    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=PENDING)
    failure_reason = models.CharField(max_length=64, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"TXN{self.pk} {self.status} {self.amount_minor}"


class LedgerEntry(models.Model):
    """A single debit or credit leg. Every Transaction produces entries in
    balanced debit/credit pairs -- never one without the other, and never
    an amount that doesn't match its pair. This is what makes "debits
    equal credits" readable straight off the schema rather than asserted
    in application code alone: see tests/test_ledger.py.
    """

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    ENTRY_TYPES = [(DEBIT, "Debit"), (CREDIT, "Credit")]

    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="entries")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=8, choices=ENTRY_TYPES)
    amount_minor = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "created_at"]),
        ]

    def __str__(self):
        return f"{self.entry_type} {self.amount_minor} {self.account}"
