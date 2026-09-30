"""
Shared scenario for the direct mode suite.

Every test here runs the real contract inside the real GenVM. Only two things are
substituted: the HTTP fetch of the evidence document and the model call. Both sit at
the network boundary, which is exactly what a test must control to make a
non-deterministic contract reproducible.

The running scenario is a medical licence gate, because it is the case where
deterministic access control genuinely cannot express the requirement: "an actively
licensed physician in good standing" is a statement about the world, not about token
balances.
"""

import json
import sys

import pytest

# A fixed point in time so TTL arithmetic in tests is exact.
BASE_TIME = "2026-01-15T12:00:00Z"

GATE_ID = "med-licence"
GATE_TITLE = "Licensed Medical Professionals"

POLICY_TEXT = (
    "Access is limited to individuals who hold a currently active licence to practise "
    "medicine, who are board certified in a recognised clinical specialty, and who "
    "have no disciplinary action or sanction recorded against that licence."
)

CONDITIONS = [
    {
        "id": "c1_active_licence",
        "text": "The evidence shows a currently active licence to practise medicine",
    },
    {
        "id": "c2_board_certified",
        "text": "The evidence shows board certification in a recognised clinical specialty",
    },
]

DISQUALIFIERS = [
    {
        "id": "d1_disciplinary",
        "text": "The evidence shows a disciplinary action, suspension or sanction",
    }
]

ALLOWED_HOSTS = ["registry.example.com", "boards.example.org"]

EVIDENCE_URL = "https://registry.example.com/licence/884213"
SECOND_URL = "https://boards.example.org/certification/884213"

BOND = 10**16              # 0.01 GEN
CHALLENGE_STAKE = 2 * 10**16  # 0.02 GEN
TTL = 86400
COOLDOWN = 3600

FUNDING = 10**19  # 10 GEN per test account

ZERO_ADDRESS = b"\x00" * 20


def hex_of(address) -> str:
    """
    Normalize an address to a lowercase hex string.

    The direct harness hands back raw 20 byte addresses while the contract returns
    checksummed hex, so comparisons go through this.
    """
    if isinstance(address, (bytes, bytearray)):
        return "0x" + bytes(address).hex()
    as_hex = getattr(address, "as_hex", None)
    if as_hex is not None:
        return str(as_hex).lower()
    return str(address).lower()


def licence_page(binding_token: str, active: bool = True, disciplined: bool = False) -> str:
    """Build a registry page. The binding token is what ties it to one wallet."""
    status = (
        "Licence status: ACTIVE and in good standing since 14 March 2011."
        if active
        else "Licence status: EXPIRED as of 31 December 2023. Not eligible to practise."
    )
    discipline = (
        "Disciplinary record: one suspension recorded 2019 following a board review."
        if disciplined
        else "Disciplinary record: none on file."
    )
    return "\n".join(
        [
            "State Medical Board of Record. Public licence verification.",
            "Licensee: Jane Okafor, MD. Licence number IM-884213.",
            status,
            "Board certification: American Board of Internal Medicine, internal medicine.",
            discipline,
            "Wallet verification token: " + binding_token,
        ]
    )


def model_findings(
    c1="SATISFIED",
    c2="SATISFIED",
    disciplinary=False,
    c1_quote="Licence status: ACTIVE and in good standing since 14 March 2011.",
    c2_quote="Board certification: American Board of Internal Medicine, internal medicine.",
) -> str:
    """A well formed model response reporting per condition findings."""
    return json.dumps(
        {
            "conditions": [
                {
                    "id": "c1_active_licence",
                    "verdict": c1,
                    "quote": c1_quote if c1 == "SATISFIED" else "",
                    "note": "board record consulted",
                },
                {
                    "id": "c2_board_certified",
                    "verdict": c2,
                    "quote": c2_quote if c2 == "SATISFIED" else "",
                    "note": "certification line present",
                },
            ],
            "disqualifiers": [
                {
                    "id": "d1_disciplinary",
                    "present": disciplinary,
                    "note": "suspension recorded" if disciplinary else "none on file",
                }
            ],
            "reasoning": "Findings derived from the public board record.",
        }
    )


def serve_page(direct_vm, body: str, url: str = EVIDENCE_URL, status: int = 200) -> None:
    """Install a web mock for one evidence URL."""
    direct_vm.mock_web(_url_pattern(url), {"status": status, "body": body})


def _url_pattern(url: str) -> str:
    return url.replace(".", r"\.").replace("/", r"\/") + ".*"


def serve_model(direct_vm, response: str) -> None:
    direct_vm.mock_llm(r".*impartial access adjudicator.*", response)


def grant_everything(direct_vm, binding_token: str) -> None:
    """The common happy path setup: retrievable evidence and satisfied findings."""
    serve_page(direct_vm, licence_page(binding_token))
    serve_model(direct_vm, model_findings())


def warp_time(direct_vm, timestamp: str) -> None:
    """
    Move the clock the contract actually reads.

    The contract takes its time from gl.message_raw["datetime"], which is the
    consensus supplied transaction time and the only clock that is identical across
    validators. The harness's warp() updates its own record and the patched
    datetime.now(), but its message refresh only rewrites sender and origin, so the
    live message_raw["datetime"] a contract reads never moves. This completes the
    cheatcode instead of changing the contract to read a weaker clock.
    """
    direct_vm.warp(timestamp)
    module = sys.modules.get("genlayer.gl")
    if module is not None and getattr(module, "message_raw", None) is not None:
        module.message_raw["datetime"] = timestamp


@pytest.fixture
def registry(direct_vm, direct_deploy, direct_owner):
    """A deployed registry with time pinned and the deployer funded."""
    contract = direct_deploy("contracts/semantic_access_gate.py")
    warp_time(direct_vm, BASE_TIME)
    direct_vm.deal(direct_owner, FUNDING)
    return contract


@pytest.fixture
def funded(direct_vm, direct_alice, direct_bob, direct_charlie):
    """Fund the three test identities so they can post bonds and stakes."""
    for address in (direct_alice, direct_bob, direct_charlie):
        direct_vm.deal(address, FUNDING)
    return True


def register_default_gate(
    contract,
    direct_vm,
    owner,
    gate_id: str = GATE_ID,
    bond: int = BOND,
    stake: int = CHALLENGE_STAKE,
    ttl: int = TTL,
    cooldown: int = COOLDOWN,
    hosts=None,
    fetch_mode: str = "raw",
    binding_required: bool = True,
    grounded: bool = True,
) -> str:
    """Register the running scenario's gate and return its id."""
    direct_vm.sender = owner
    return contract.register_gate(
        gate_id,
        GATE_TITLE,
        POLICY_TEXT,
        json.dumps(CONDITIONS),
        json.dumps(DISQUALIFIERS),
        json.dumps(ALLOWED_HOSTS if hosts is None else hosts),
        fetch_mode,
        binding_required,
        grounded,
        ttl,
        bond,
        stake,
        cooldown,
    )


@pytest.fixture
def gate(registry, direct_vm, direct_owner, funded):
    """A registered gate owned by direct_owner, ready to receive applications."""
    register_default_gate(registry, direct_vm, direct_owner)
    return registry


def apply_as(contract, direct_vm, applicant, urls=None, note="Internal medicine.", bond=BOND, gate_id=GATE_ID):
    """Submit an application as `applicant` with the gate's bond attached."""
    direct_vm.sender = applicant
    direct_vm.value = bond
    try:
        return contract.apply_for_access(
            gate_id, json.dumps(urls or [EVIDENCE_URL]), note
        )
    finally:
        direct_vm.value = 0
