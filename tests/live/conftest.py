"""
Shared machinery for the live StudioNet suite.

These tests execute against the deployed contracts on the GenLayer Studio network.
Adjudications here are real: real validators fetch real documents over real HTTPS and
prompt real language models, and consensus really has to agree before anything is
written. Nothing in this directory is mocked.

Two kinds of evidence are used, for two different reasons.

Real world content. The public README of a GitHub repository, fetched from
raw.githubusercontent.com. Nobody involved in this test wrote it for the test, which is
what makes it a fair check that validators can retrieve and reason about a document in
the wild.

Controlled content. A document encoded into a URL and echoed back verbatim by a public
service. The fetch, the host, the TLS and the adjudication are all real; the only thing
under test control is what the document says. This is required to exercise the binding
token path, because a wallet proof has to appear in the document and no third party page
is going to contain this deployer's address.
"""

import base64
import json
import os
import pathlib
import time
import urllib.request

import pytest
from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ARTIFACT = ROOT / "deployments" / "studionet.json"

# Keys come from the environment and are deliberately absent from the source. The live
# suite acts as the gate owner, so it needs a funded account; without the variable set the
# whole directory skips rather than failing with a confusing signing error.
#
# GENLAYER_DEPLOYER_KEY  the account that owns the gates, required
# GENLAYER_SECOND_KEY    a second identity for the non owner and impersonation tests,
#                        defaulting to a well known public development key
DEPLOYER_KEY = os.environ.get("GENLAYER_DEPLOYER_KEY", "").strip()
SECOND_KEY = os.environ.get(
    "GENLAYER_SECOND_KEY",
    "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d",
).strip()

RPC_URL = "https://studio.genlayer.com/api"
USER_AGENT = "semantic-access-gate-tests/1.0"

EVIDENCE_HOST_REAL = "raw.githubusercontent.com"
EVIDENCE_HOST_ECHO = "httpbin.org"

README_URL = (
    "https://raw.githubusercontent.com/dotmantissa/GenLayer-Primitives/main/README.md"
)
MISSING_URL = (
    "https://raw.githubusercontent.com/dotmantissa/GenLayer-Primitives/main/"
    "this-file-does-not-exist-9f3a2b.md"
)

# StudioNet allows 60 requests a minute, 1000 an hour, and 32 transactions in flight per
# sender. A live suite makes hundreds of calls, so every request is spaced and every
# request retries on a limiter response. Without this a rate limit surfaces as a fixture
# error and every dependent test reports a failure that has nothing to do with the
# contract.
WRITE_PAUSE_SECONDS = 2.0
RATE_LIMIT_ATTEMPTS = 6
RATE_LIMIT_BACKOFF_SECONDS = 12

RATE_LIMIT_MARKERS = ("-32429", "-32028", "-32029", "rate limit", "too many", "429")

# Receipt polling is the dominant consumer of the request budget: every poll is a
# request, and the observed hourly ceiling is 500. A six second interval cuts the polls
# per transaction to a handful without meaningfully slowing a suite whose transactions
# take tens of seconds anyway.
RECEIPT_RETRIES = 60
RECEIPT_INTERVAL_MS = 6000


def echo_url(document: str) -> str:
    """
    Publish a document as a URL that returns it verbatim.

    Padding is kept: the service rejects unpadded base64, which is easy to get wrong and
    produces a page containing an error message rather than the intended evidence.
    """
    encoded = base64.urlsafe_b64encode(document.encode("utf-8")).decode("ascii")
    return "https://httpbin.org/base64/" + encoded


def fetch_text(url: str, timeout: int = 25) -> str:
    """Fetch a URL from the test process, to confirm what validators will see."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def with_retry(call, description: str):
    """
    Run a network call, backing off when the node reports a rate limit.

    Only limiter responses are retried. A contract revert or an assertion failure is
    raised immediately, so this never hides a real result behind a retry loop.
    """
    last = None
    for attempt in range(RATE_LIMIT_ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            last = exc
            time.sleep(RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
    raise AssertionError(
        f"{description} still rate limited after {RATE_LIMIT_ATTEMPTS} attempts: {last}"
    )


def _leader_receipts(receipt: dict) -> list:
    consensus = receipt.get("consensus_data") or {}
    leader = consensus.get("leader_receipt") or []
    if isinstance(leader, dict):
        return [leader]
    return list(leader)


# Reading a StudioNet receipt correctly is subtle enough to be worth spelling out.
#
# The top level `status`, `status_name` and `result` fields do not distinguish a
# successful call from a reverted one: a successful write and a reverted write both come
# back ACCEPTED with the same result code. Only the leader receipts carry the outcome.
#
# `consensus_data.leader_receipt` is a list holding every leader attempt for the
# transaction, including attempts that failed and caused a rotation. A transaction whose
# first leader errored and whose second succeeded applies its state and has both an
# error entry and a success entry, so treating any error entry as fatal reports a failure
# for a transaction that consensus resolved. A transaction therefore succeeded if at
# least one attempt reports SUCCESS, and failed only if none did.
#
# A revert reports status "rollback" with the contract's message in `result.payload` and
# an empty stderr. A call that returned a value reports "return", one that returned
# nothing reports "success" or "none".
SUCCESS_RESULT_STATUSES = ("return", "success", "none", "")


def _entry_payload(entry: dict) -> str:
    result = entry.get("result") or {}
    payload = result.get("payload", "")
    if isinstance(payload, dict):
        payload = payload.get("readable", json.dumps(payload))
    return str(payload)


def _entry_succeeded(entry: dict) -> bool:
    execution = str(entry.get("execution_result", "")).upper()
    status = str((entry.get("result") or {}).get("status", "")).lower()
    if execution and execution != "SUCCESS":
        return False
    return status in SUCCESS_RESULT_STATUSES


def execution_failure(receipt: dict) -> str:
    """
    Return a description of why a transaction failed to execute, or an empty string.

    A StudioNet transaction can reach ACCEPTED or FINALIZED while the contract call
    inside it raised. Consensus finalized a failure, which is a correct outcome for the
    chain and a silent disaster for a test that only checks the status name. Every write
    in this suite goes through here.
    """
    entries = _leader_receipts(receipt)
    if not entries:
        return "no leader receipt was returned for this transaction"
    if any(_entry_succeeded(entry) for entry in entries):
        return ""

    # Pick the entry that carries the contract's own message. When consensus cancels
    # validators after quorum their entries survive in the list with the payload "idle",
    # and taking the last entry would report that instead of the revert reason. A
    # rollback entry holds the message, so it is preferred.
    def rank(entry: dict) -> int:
        status = str((entry.get("result") or {}).get("status", "")).lower()
        payload = _entry_payload(entry).strip().lower()
        if status == "rollback":
            return 0
        if payload and payload != "idle":
            return 1
        return 2

    best = min(entries, key=rank)
    execution = str(best.get("execution_result", "")).upper()
    status = str((best.get("result") or {}).get("status", "")).lower()
    stderr = (best.get("genvm_result") or {}).get("stderr") or ""
    return (
        f"execution_result={execution} status={status} {_entry_payload(best)}"
        f"\n{stderr[-1500:]}"
    ).strip()


def send(client, address: str, function_name: str, args: list, value: int = 0) -> dict:
    """Submit a write, wait for its receipt, and fail loudly if execution errored."""
    tx_hash = with_retry(
        lambda: client.write_contract(
            address=address, function_name=function_name, args=args, value=value
        ),
        f"write {function_name}",
    )
    receipt = with_retry(
        lambda: client.wait_for_transaction_receipt(
            tx_hash, retries=RECEIPT_RETRIES, interval=RECEIPT_INTERVAL_MS
        ),
        f"receipt for {function_name}",
    )
    failure = execution_failure(receipt)
    if failure:
        raise AssertionError(f"{function_name}{args!r} failed on chain:\n{failure}")
    time.sleep(WRITE_PAUSE_SECONDS)
    return receipt


def expect_revert(client, address: str, function_name: str, args: list, value: int = 0):
    """
    Assert that a write is rejected on chain, and return the failure text.

    Rejection can surface either as a refused submission or as a finalized transaction
    whose execution raised, and a caller should not have to care which.
    """
    try:
        tx_hash = with_retry(
            lambda: client.write_contract(
                address=address, function_name=function_name, args=args, value=value
            ),
            f"write {function_name}",
        )
    except AssertionError:
        raise
    except Exception as exc:
        time.sleep(WRITE_PAUSE_SECONDS)
        return str(exc)

    receipt = with_retry(
        lambda: client.wait_for_transaction_receipt(
            tx_hash, retries=RECEIPT_RETRIES, interval=RECEIPT_INTERVAL_MS
        ),
        f"receipt for {function_name}",
    )
    failure = execution_failure(receipt)
    time.sleep(WRITE_PAUSE_SECONDS)
    if not failure:
        raise AssertionError(
            f"{function_name}{args!r} was expected to be rejected but succeeded"
        )
    return failure


def read(client, address: str, function_name: str, args: list):
    return with_retry(
        lambda: client.read_contract(
            address=address, function_name=function_name, args=args
        ),
        f"read {function_name}",
    )


def read_json(client, address: str, function_name: str, args: list):
    raw = read(client, address, function_name, args)
    if raw == "":
        return None
    return json.loads(raw)


def fund(address: str, amount_wei: int) -> None:
    """Top up an account through the Studio funding method."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "sim_fundAccount",
            "params": [address, amount_wei],
            "id": 1,
        }
    ).encode()
    # An explicit user agent is required: the gateway refuses Python's default with a
    # 403, which surfaces as an unrelated looking failure inside a fixture.
    request = urllib.request.Request(
        RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        json.loads(response.read().decode())


@pytest.fixture(scope="session")
def deployment():
    if not ARTIFACT.exists():
        pytest.skip("deployments/studionet.json is missing; run scripts/deploy.py first")
    return json.loads(ARTIFACT.read_text())


@pytest.fixture(scope="session")
def registry_address(deployment):
    return deployment["registryAddress"]


@pytest.fixture(scope="session")
def consumer_address(deployment):
    return deployment["consumerAddress"]


@pytest.fixture(scope="session")
def owner_account():
    if not DEPLOYER_KEY:
        pytest.skip(
            "set GENLAYER_DEPLOYER_KEY to a funded StudioNet account to run the live suite"
        )
    return create_account(DEPLOYER_KEY)


@pytest.fixture(scope="session")
def owner(owner_account):
    return create_client(chain=studionet, account=owner_account)


@pytest.fixture(scope="session")
def other_account():
    return create_account(SECOND_KEY)


@pytest.fixture(scope="session")
def other(other_account, owner):
    """A second identity, funded so it can post bonds and stakes of its own."""
    client = create_client(chain=studionet, account=other_account)
    if client.get_balance(other_account.address) < 10**18:
        fund(other_account.address, 10**19)
        time.sleep(3)
    return client


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# The gate the deployed consumer contract is bound to. Its policy is about the content
# of a real public document, and its evidence host is the one serving that document, so
# the composability demonstration does not depend on any service this suite controls.
CONSUMER_GATE_POLICY = (
    "Access is limited to applicants whose evidence document establishes that the "
    "software it describes runs contract code on a blockchain and reaches its results "
    "through consensus among independent validators rather than through a single "
    "trusted operator. Evidence describing abandoned or deprecated software is refused."
)

CONSUMER_GATE_CONDITIONS = [
    {
        "id": "c1_executes_code",
        "text": "The document states that contracts execute program code on chain",
    },
    {
        "id": "c2_validator_consensus",
        "text": "The document states that results are reached through validator consensus",
    },
]

CONSUMER_GATE_DISQUALIFIERS = [
    {
        "id": "d1_deprecated",
        "text": "The document states that the software is deprecated, abandoned or unmaintained",
    }
]

# A gate that requires proof of wallet control, used to exercise the binding token path.
BOUND_GATE_POLICY = (
    "Access is limited to applicants who publish evidence of an active professional "
    "licence in good standing, and who prove control of the applying wallet by "
    "publishing the gate's binding token on that same evidence document."
)

BOUND_GATE_CONDITIONS = [
    {
        "id": "c1_active_licence",
        "text": "The document shows a currently active professional licence in good standing",
    }
]

BOUND_GATE_DISQUALIFIERS = [
    {
        "id": "d1_suspended",
        "text": "The document shows the licence is suspended, revoked or under sanction",
    }
]

LIVE_BOND = 10**15            # 0.001 GEN
LIVE_STAKE = 2 * 10**15       # 0.002 GEN
LIVE_TTL = 86400


def licence_document(binding_token: str, active: bool = True) -> str:
    """A licence record carrying a wallet binding token, for the controlled evidence host."""
    status = (
        "Licence status: ACTIVE and in good standing since 14 March 2011."
        if active
        else "Licence status: EXPIRED on 31 December 2023 and not eligible to practise."
    )
    return "\n".join(
        [
            "State Professional Standards Board. Public licence verification record.",
            "Licensee: Jane Okafor. Licence number IM-884213.",
            status,
            "Sanctions and disciplinary record: none on file for this licensee.",
            "Wallet verification token: " + binding_token,
            "",
        ]
    )


# Gate state read by ensure_gate, cached for the session. Only used to skip a repeat
# registration, never to answer an assertion; tests always read the chain themselves.
_GATE_CACHE: dict = {}


def unique_gate_id(prefix: str) -> str:
    """A gate id unique to this run, so the suite can be run repeatedly."""
    return f"{prefix}-{int(time.time())}"


def ensure_gate(client, registry: str, gate_id: str, **config) -> dict:
    """
    Register a gate if it does not already exist, and return its stored state.

    Registration is idempotent so the live suite can be re-run against an existing
    deployment without every test tripping over a gate id that is already taken.
    """
    if gate_id in _GATE_CACHE:
        return _GATE_CACHE[gate_id]
    existing = read_json(client, registry, "get_gate", [gate_id])
    if existing is not None:
        _GATE_CACHE[gate_id] = existing
        return existing

    send(
        client,
        registry,
        "register_gate",
        [
            gate_id,
            config["title"],
            config["policy_text"],
            json.dumps(config["conditions"]),
            json.dumps(config.get("disqualifiers", [])),
            json.dumps(config.get("allowed_hosts", [])),
            config.get("fetch_mode", "raw"),
            config.get("binding_required", False),
            config.get("require_grounded_quotes", True),
            config.get("access_ttl_seconds", LIVE_TTL),
            config.get("bond_wei", LIVE_BOND),
            config.get("challenge_stake_wei", LIVE_STAKE),
            config.get("reapply_cooldown_seconds", 0),
        ],
    )
    stored = read_json(client, registry, "get_gate", [gate_id])
    assert stored is not None, f"gate {gate_id} was not readable after registration"
    _GATE_CACHE[gate_id] = stored
    return stored


def release_if_held(client, registry: str, gate_id: str, address: str) -> bool:
    """
    Give up any open access record the address holds, so a repeat run starts clean.

    The condition is that the record is ACTIVE, not that it is currently approved. A
    record invalidated by a policy change is still ACTIVE and still holds the applicant's
    deposit, so keying on `approved` would leave that money locked and grow the registry's
    locked total on every run. A record with an open challenge cannot be released, and is
    left alone.

    Returns True if a release was performed.
    """
    record = read_json(client, registry, "get_access_record", [gate_id, address])
    if record is None or record.get("status") != "ACTIVE":
        return False
    status = read_json(client, registry, "access_status", [gate_id, address])
    if status and status.get("has_open_challenge"):
        return False
    send(client, registry, "release_access", [gate_id])
    return True


def apply_and_get_id(client, registry: str, gate_id: str, urls: list, note: str, bond: int):
    """
    Submit an application and return its id.

    The id is read back from the applicant's own index rather than decoded from the
    receipt, which keeps this independent of how a client encodes return values.
    """
    address = client.local_account.address
    before = read_json(client, registry, "get_applicant_applications", [address, 0, 1000])
    send(
        client,
        registry,
        "apply_for_access",
        [gate_id, json.dumps(urls), note],
        value=bond,
    )
    after = read_json(client, registry, "get_applicant_applications", [address, 0, 1000])
    added = [
        app_id
        for app_id in after["application_ids"]
        if app_id not in set(before["application_ids"])
    ]
    assert len(added) == 1, f"expected exactly one new application, saw {added}"
    return added[0]


@pytest.fixture(scope="session")
def consumer_gate(owner, registry_address, deployment):
    """The gate the deployed consumer defers to, registered against real world evidence."""
    gate_id = deployment["consumerBoundGateId"]
    ensure_gate(
        owner,
        registry_address,
        gate_id,
        title="Verified Consensus Software Operators",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        disqualifiers=CONSUMER_GATE_DISQUALIFIERS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        require_grounded_quotes=True,
    )
    return gate_id


@pytest.fixture(scope="session")
def bound_gate(owner, registry_address):
    """A gate requiring proof of wallet control, on the controlled evidence host."""
    gate_id = "wallet-bound-licence"
    ensure_gate(
        owner,
        registry_address,
        gate_id,
        title="Wallet Bound Licence Holders",
        policy_text=BOUND_GATE_POLICY,
        conditions=BOUND_GATE_CONDITIONS,
        disqualifiers=BOUND_GATE_DISQUALIFIERS,
        allowed_hosts=[EVIDENCE_HOST_ECHO],
        binding_required=True,
        require_grounded_quotes=True,
    )
    return gate_id
