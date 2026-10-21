# Airtime vending service

Take-home for CIMG/ENG/EX/2026/002 (Senior Software Engineer, Platforms & Integrations).

## A note on the scaffold

I emailed to ask about the scaffold referenced in the brief ("We have provided the operator as a
running service in the scaffold"). The response: "The scenario" section frames the exercise as if
a scaffold, operator service, USSD gateway, and Docker Compose environment are available as
described — but that framing is part of the exercise's premise, not a literal set of files being
shipped separately. So `operator/` and `ussd-gateway/driver.py` in this repo are **built from
scratch by me**, not a stand-in for something missing — they're my own implementation of "the
operator" and the gateway test harness the brief describes, designed to exhibit the failure
taxonomy it calls for (clear failures, transient errors, and — the hard one — a timeout that
occurs after the vend already committed on the operator's side). Their docstrings explain the
design choices behind each failure bucket.

Because I designed the operator's contract myself, `OperatorClient`
(`service/vending/operator_client.py`) is the one seam in this codebase that's shaped by an
assumption rather than a spec: everything it talks to is `operator/`, a small Django project in
this same repo (a second Django project alongside `service/` on purpose — Python/DRF is the
stack I'm fastest in, so both halves of this exercise use it rather than reaching for a second
framework for the mock), so there's no external contract drift to worry about. But it also means
the interesting judgment call here is less "did I integrate against the given API correctly" and
more "did I design a plausible one and then handle everything it can throw." The `/ussd` contract
(form-encoded `sessionId`/`serviceCode`/`phoneNumber`/`text` in, `CON `/`END ` plain text out)
follows the Africa's Talking-style convention most Nigerian short codes actually run on.

## How to run it

```
make up
```

(equivalent to `docker compose up`, see `Makefile`; `make down` tears it back down). This brings
up Postgres, the stand-in operator (port 9000), and the vending service (port 8000), running
migrations automatically. Try it:

```
curl -X POST http://localhost:8000/ussd \
  -d sessionId=demo1 -d serviceCode=*123# -d phoneNumber=2348099990000 -d text=
# CON Welcome to Airtime Vending
# 1. Buy airtime

curl -X POST http://localhost:8000/ussd \
  -d sessionId=demo1 -d serviceCode=*123# -d phoneNumber=2348099990000 -d text=1*2348011112222*500*1
# END Top-up of NGN500 to 2348011112222 was successful. Ref: TXN1
```

Any agent phone number that dials in gets an auto-provisioned float of NGN 50,000 the first
time it's seen (see "What I deliberately did not build" — real agent onboarding is out of scope).
The **customer** phone number's last digit selects the stand-in operator's behavior — see
`operator/mockoperator/views.py` for the full table, summarized here:

| last digit | behavior |
|---|---|
| 0, 7 | success |
| 1 | clean failure: insufficient operator float |
| 2 | clean failure: invalid number |
| 3 | clean failure: transient error (503) |
| 4 | **times out on our side, but commits SUCCESS on the operator's side** |
| 5 | times out on our side, resolves to FAILED on the operator's side |
| 6 | times out and never resolves at all |
| 8 | slow (3s) but succeeds — inside the operator-call budget |
| 9 | clean failure: rate limited (429) |

### Environment I actually validated this in

Docker Hub (and the public ECR mirror) was unreachable from this sandbox's network — image
pulls timed out even for `alpine`. `docker-compose.yml` is standard and there's no reason to
expect it not to work in a normal environment, but I want to be upfront that I could not
personally exercise `docker compose up` end to end. What I did instead, to make sure the actual
system works and not just its unit tests: ran a local Postgres and both Django projects
(`operator/manage.py runserver` and `service/manage.py runserver`) as three plain processes, and
ran `ussd-gateway/driver.py --scenario all` against them over real HTTP. All six scenarios passed
(output below). If Docker doesn't come up cleanly in review, this is the fallback and it's a good
sign the compose file's application code is sound even if I couldn't prove the container wiring
itself.

```
PASS  happy_path
PASS  gateway_retry
PASS  double_tap_confirm
PASS  concurrent_duplicate
PASS  operator_failure_shapes
PASS  session_expiry
all scenarios passed
```

## How to run the tests

Requires Postgres (money-correctness tests rely on real row locking — see
`service/config/settings.py` for why SQLite won't do).

From a fresh clone, with Python 3.12:

```
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r service/requirements.txt
docker compose up -d postgres   # or any Postgres 14+ reachable via the POSTGRES_* vars
make test                       # = cd service && python manage.py test vending
make lint                       # = ruff check service operator ussd-gateway (pip install ruff first)
```

`make test` uses the same defaults as `docker-compose.yml` (`airtime`/`airtime` on
`localhost:5432`); override any of them with the variables listed in `.env.example`.

### Dependencies

Each project (`service/`, `operator/`, `ussd-gateway/`) declares its direct dependencies in
`requirements.in`; `requirements.txt` beside it is the lockfile, compiled by `pip-compile` with
every transitive dependency pinned and hashed. To change a dependency, edit the `.in` file and run
`make lock` (needs `pip install pip-tools`). Dependabot proposes weekly updates.

### CI

`.github/workflows/ci.yml` runs on every push and pull request: ruff, `pip-audit` against all
three lockfiles, the test suite above against a `postgres:16` service container (plus a
missing-migrations check), and an end-to-end job that starts both Django projects and runs
`ussd-gateway/driver.py --scenario all` over real HTTP.

29 tests, ~8s. The one the brief says will be checked first is
`vending/tests/test_concurrency.py` — it fires 12 threads at the identical confirm callback for
one session and asserts exactly one `Transaction` and one balanced ledger pair exist afterwards.
It uses `TransactionTestCase`, not `TestCase`, deliberately: Django's regular `TestCase` wraps
each test in one outer transaction that isn't visible across threads, which would hide the exact
race being tested.

`vending/tests/test_timeout_integration.py` spawns `operator/manage.py runserver` as a real
subprocess (both services are Django projects, so no extra dependency is needed for this) and
proves the timeout-after-commit case end to end, not against a mock: the client times out in
0.3s, and a follow-up `/operator/status` query 1.5s later shows the vend had in fact succeeded.
Skipped automatically if `operator/manage.py` can't be found.

## The USSD timing budget

The gateway gives the whole request 8 seconds. Only the confirm step does anything slow (the
operator call); every other menu step is a local DB read/write with no network call, so it has
no meaningful bearing on the budget.

**Decision: the operator call happens synchronously inside the request, with a 4.5s client-side
timeout** (`OPERATOR_CALL_TIMEOUT_SECONDS`), leaving headroom for our own DB work and network
transit to/from the gateway. I did not confirm success to the subscriber before knowing the
operator's answer, and I did not make the USSD session hold open past 8 seconds waiting for a
slow operator.

Why not respond `CON Please wait...` and poll on a later callback instead? That's the standard
pattern for genuinely long-running actions, but it doesn't fit the vend confirm step cleanly:
the subscriber didn't provide any new input to drive a next `text` value, and Africa's
Talking-style gateways don't support the server pushing an unsolicited follow-up into an open
session. It would require inventing a synthetic "check status" menu branch and asking the
subscriber to navigate back into it, which is worse UX for a step that resolves within a few
seconds the overwhelming majority of the time. The 4.5s budget covers the "slow but fine" case
(bucket 8, 3s) comfortably; the cases that actually blow the budget are exactly the ones where we
*shouldn't* be telling the subscriber anything confident anyway (see next section).

## Handling the operator's failures

| Shape | How it's told apart | Transaction outcome |
|---|---|---|
| Clean failure (bad number, no float, 503, 429) | Operator responds within budget with a definitive answer | `FAILED`, reservation reversed, funds returned to agent |
| Success | Operator responds within budget with `SUCCESS` | `SUCCESS`, reservation stands |
| **Timeout after the vend may have already committed** | The HTTP call itself times out or the connection drops | `NEEDS_RECONCILIATION` — **not resolved by guessing** |

The third row is the one the brief calls out specifically, and it's the one place in this design
I think matters most. `OperatorClient.vend()` (`service/vending/operator_client.py`) returns one
of three dataclasses — `OperatorSuccess`, `OperatorFailure`, `OperatorUnknown` — and a client
timeout or connection error can only ever produce `OperatorUnknown`. It is structurally
impossible for a timeout to be reported as `OperatorFailure`: reversing the ledger reservation
for a vend that may have actually gone through would mean the agent both keeps their float *and*
the customer got airtime — a real leak of money, and a much worse outcome than a transaction
that sits unresolved. `vending/tests/test_operator_client.py` asserts this directly, and
`test_timeout_integration.py` proves it against the real (stand-in) operator process rather than
a stubbed response.

A `NEEDS_RECONCILIATION` transaction is a genuine terminal-but-unresolved state, not a stranded
one — see "Reconciliation" below for the explicit path out of it.

## Idempotency: no double vends

Two things can produce a duplicate confirm callback for one session: the gateway retrying
because it didn't see an ACK in time, and a subscriber pressing "confirm" twice. Both look
identical at the HTTP layer (same `sessionId`, same accumulated `text`), so they're handled by
the same guarantee: **at most one `Transaction` can ever be attached to a `UssdSession`.**

The mechanism (`vending/ussd.py::_claim_transaction`):
1. `SELECT ... FOR UPDATE` the session row — this serializes concurrent attempts on the *same*
   session, without holding any lock across the outbound operator call (the lock is released as
   soon as the `Transaction` and its ledger entries are committed; the operator HTTP call happens
   afterwards, unlocked).
2. If the session already has a transaction, return it — the caller renders whatever its current
   state is (see below) instead of creating anything new or calling the operator again.
3. Otherwise create the `Transaction`, post the ledger reservation, and link it to the session in
   the same atomic block.

The `OneToOneField` from `UssdSession` to `Transaction` is the actual backstop underneath the row
lock: it puts a database-level unique constraint on the link, so even a bug in the locking logic
above could not result in two transactions attached to one session — the second insert would
simply fail.

One subtlety worth being explicit about: a confirm that arrives *while the first one is still
mid-flight* (between the reservation committing and the operator call returning) will see the
transaction already exists but still `PENDING`, and gets told "your request is still being
processed" rather than the eventual outcome. That's a different message from the one that
resolves the vend, but it references the same transaction reference — no second vend, no lost
information, just an honest "I don't know yet" for the request that lost the race. Both
`vending/tests/test_concurrency.py` and `ussd-gateway/driver.py`'s `concurrent_duplicate` scenario
assert on the transaction reference rather than exact text equality for this reason.

## Ledger

Two account types (`vending/models.py::Account`), both modeled as liabilities:

- **Agent wallet** — the float the platform owes back to the agent as spendable value. Credited
  when funded (out of scope, seeded directly); debited when spent on a vend.
- **Operator clearing** — what the platform owes the *operator* for airtime delivered but not
  yet paid for on their daily settlement cycle. Credited when a vend is reserved, debited back if
  that vend fails.

A vend reservation is one balanced pair: **debit** the agent's wallet, **credit** operator
clearing, both for `amount_minor`. A failure posts the exact reversing pair. A success or a
`NEEDS_RECONCILIATION` outcome posts nothing further — the original pair stands either way, which
is the point: **the ledger footprint of a successful vend and an unresolved one is identical.**
They are told apart purely by `Transaction.status`, never inferred from timestamps or entry
counts — this is the literal thing the brief asks for, and it's asserted directly in
`test_ledger.py::NeedsReconciliationLedgerTests`.

All amounts are `PositiveBigIntegerField`/`BigIntegerField` in kobo (minor units) — no float or
`Decimal` anywhere near a balance. `test_ledger.py::DoubleEntrySchemaTests` sums every debit and
every credit across the whole ledger and asserts equality, and checks per-transaction that every
row's debit and credit legs match — the "read the schema and see debits equal credits" ask, made
into an actual assertion rather than a claim.

## Reconciliation (designed, not built, per the brief)

`OperatorClient.status()` already exists and is tested (`test_timeout_integration.py`) — it's the
one call a reconciliation job needs and nothing else. The job itself, which the brief explicitly
asks not to build:

1. Poll `Transaction.objects.filter(status__in=[PENDING, NEEDS_RECONCILIATION], created_at__lt=cutoff)`
   on an interval (a `PENDING` past a minute or two almost certainly means the process crashed
   between reserving funds and calling the operator — same resolution path applies).
2. For each, call `OperatorClient.status(reference=txn.operator_reference)`.
3. `OperatorSuccess` → mark `SUCCESS`, leave the ledger as posted. `OperatorFailure` → run the
   same `_reverse_reservation` used in the synchronous path, mark `FAILED`. `OperatorUnknown`
   (operator still doesn't know, or errors) → leave it, but if it's been unresolved past some
   longer threshold (a day?), escalate to a human queue rather than looping forever — bucket 6 in
   the stand-in operator (never resolves) exists specifically to represent that this path is
   real and needs a floor, not just a retry loop.
4. The operator's daily settlement file is the independent second source of truth for this same
   reconciliation — the interview brief says this will be discussed live, so I've deliberately
   left it unopened and undesigned rather than guess at its shape ahead of that conversation.

This wants to be a small standalone management command or worker, run on a schedule, sharing
`_reverse_reservation` and `OperatorClient.status` with the request path rather than duplicating
either.

## What I deliberately did not build

- **A background worker/scheduler** — the brief asks for the reconciliation *design*, not the
  worker. Every piece it would call (`OperatorClient.status`, `_reverse_reservation`) already
  exists and is tested in isolation.
- **Agent onboarding/funding** — an agent wallet is auto-provisioned with a demo float the first
  time its phone number is seen. Real float funding is a payment-rail integration with its own
  failure modes that deserve their own exercise, not a rushed add-on to this one.
- **Auth beyond what's asked** — the only endpoint is the `/ussd` gateway callback, and USSD
  aggregators don't attach bearer tokens; that trust boundary is normally an IP allowlist or
  mutual TLS at the infra layer. `settings.API_KEY` exists as a placeholder for a future
  JSON/ops endpoint (e.g. a manually-triggered reconciliation run), per the brief's allowance for
  shared-key auth, but nothing currently checks it — there's nothing yet worth gating.
- **A cap on total exposure per agent or globally** — real fraud/limits controls (velocity
  limits, per-agent daily caps) are a product decision I don't have inputs for here.
- **Retrying a `FAILED` vend automatically** — a subscriber whose vend failed cleanly has to dial
  in again. Auto-retry inside the same session would blur "did this fail" with "are we trying
  again," which is exactly the ambiguity the ledger design is trying to avoid.
- **Multi-agent, multi-operator, i18n of the menu copy** — explicitly out of scope.

## What I'd do with two more weeks, in priority order

1. **Build the reconciliation job** as designed above, plus a small `--dry-run` mode so it can be
   demoed against the settlement file once its shape is known.
2. **Idempotency at the operator-call layer itself, not just the session layer.** Right now a
   process crash between committing the `PENDING` transaction and calling the operator leaves a
   transaction that reconciliation would need to pick up by *calling* the operator for the first
   time (via `/operator/status`, which would correctly say "never heard of this reference") —
   fine, but I'd want an explicit "first attempt vs. resumed attempt" distinction in the log/ledger
   trail so it's visibly different from a timeout-driven `NEEDS_RECONCILIATION`.
3. **Structured (JSON) logging** instead of the current formatted-string logging — same
   information, but greppable/queryable by `transaction_id` in a real log aggregator rather than
   by regex.
4. **Load/soak testing the concurrency guard** beyond 12 threads in a unit test — I'm confident in
   the row-lock design, but I'd want to see it under sustained concurrent load against the same
   hot session and a spread of different sessions before calling it production-ready.
5. **A read endpoint for a single transaction** (status, ledger entries, human-readable timeline),
   gated by the shared API key mentioned above — mainly for support/ops to answer "what happened
   to this vend" without reaching for `psql`.
6. Revisit the max-vend-amount cap (`MAX_VEND_AMOUNT_NAIRA`, currently an arbitrary 50,000) with
   real product input.

## Where I used AI tooling

I used Claude (this session) for the full build — design discussion, the state machine and
ledger code, the test suite, and this README. One thing I rejected: my first instinct for the
`NEEDS_RECONCILIATION` case was to have `OperatorClient` immediately follow a timed-out `vend()`
call with a synchronous `status()` check before giving up, on the theory that it might resolve
things faster for the subscriber. I reversed that — it spends more of the already-tight 8-second
budget chasing an answer that isn't guaranteed to arrive quickly (the stand-in operator's
"never resolves" bucket exists precisely because a real answer sometimes isn't coming soon), and
it duplicates what a reconciliation job should own. The synchronous path now does exactly one
operator call and accepts "I don't know yet" as a valid, honest outcome.

## What I'm unhappy with

- The biggest gap: I could not run `docker compose up` end to end myself, for an environment
  reason (no Docker Hub access from this sandbox) rather than a code reason — see "Environment I
  actually validated this in" above. I'm confident in the compose file because it's about as
  plain as they come (three services, one healthcheck, no custom networking), but "confident"
  isn't the same as "watched it work," and I'd want that checked in review before trusting it.
- The USSD menu copy is functional, not polished — "Enter the customer's phone number" etc. is
  clear but not what I'd ship to real subscribers without a copy pass.
- `MAX_VEND_AMOUNT_NAIRA` and the demo starting float are both numbers I picked to make the
  system exercisable, not numbers backed by any real product requirement.
- The operator's failure buckets (keyed off the last digit of the phone number) are a
  convenience for reproducible testing, not something resembling how a real vendor API would
  actually signal these cases (real ones are typically messier: inconsistent status codes,
  vendor-specific error bodies, sometimes XML/SOAP instead of JSON). I designed a plausible
  contract rather than integrating against a given one, which means the interesting part of this
  exercise — for me, and for review — is less "did I read the docs correctly" and more "did I
  reason correctly about what a telecom integration can throw at you." I'm fairly confident the
  taxonomy (clean failure vs. transient vs. genuinely unknown) generalizes; the exact bucket
  boundaries and reason codes obviously wouldn't survive contact with a real operator unchanged.
