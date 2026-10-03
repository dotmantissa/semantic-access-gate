"""
Accounting invariants.

The contract holds other people's money in three forms: bonds on applications that
have not been adjudicated, deposits behind live access records, and stakes on open
challenges. Everything else it holds is gate treasury, which owners may withdraw.

The invariant tested here is that the contract's running totals always equal what an
independent walk of the stored records says they should. It is checked after every step
of a long mixed scenario, because an accounting error that only appears after an
unusual sequence is exactly the kind that reaches production.
"""

import json

import pytest

from conftest import (
    BOND,
    CHALLENGE_STAKE,
    COOLDOWN,
    EVIDENCE_URL,
    GATE_ID,
    TTL,
    apply_as,
    grant_everything,
    licence_page,
    model_findings,
    register_default_gate,
    serve_model,
    serve_page,
    warp_time,
)

CHALLENGE_URL = "https://boards.example.org/discipline/884213"
REASON = "Board record now shows a suspension against this licence"


def audit(contract):
    """
    Recompute the registry totals from the stored records and compare.

    Nothing here reads the contract's counters to derive the expected value; it walks
    gates, applications, access records and challenges and adds up what each one says
    it is holding. Returns the reconstructed figures so callers can assert on them.

    Archived records are walked too. They should always report a zero deposit, because
    a record is only archived after its deposit has been settled, so including them
    means a renewal that closed a record without paying it out would show up here as a
    total the live records cannot account for rather than passing unnoticed.
    """
    stats = json.loads(contract.get_registry_stats())
    gate_ids = json.loads(contract.list_gates(0, 100))["gate_ids"]

    expected_locked = 0
    expected_treasury = 0

    for gate_id in gate_ids:
        gate = json.loads(contract.get_gate(gate_id))
        expected_treasury += int(gate["treasury_wei"])

        listing = json.loads(contract.list_applications(gate_id, 0, 100))
        for app_id in listing["application_ids"]:
            app = json.loads(contract.get_application(app_id))
            if app["status"] == "PENDING":
                expected_locked += int(app["bond_wei"])

        holders = json.loads(contract.list_holders(gate_id, 0, 100))["holders"]
        for entry in holders:
            record = json.loads(contract.get_access_record(gate_id, entry["holder"]))
            expected_locked += int(record["deposit_wei"])

            history = json.loads(
                contract.get_access_history(gate_id, entry["holder"], 0, 100)
            )
            for archived in history["records"]:
                assert archived["deposit_wei"] == "0", (
                    f"archived record {archived['application_id']} still holds "
                    f"{archived['deposit_wei']} wei with no way to reclaim it"
                )
                expected_locked += int(archived["deposit_wei"])

    for index in range(int(stats["challenge_count"])):
        challenge = json.loads(contract.get_challenge("chal_" + str(index)))
        if challenge["status"] == "OPEN":
            expected_locked += int(challenge["stake_wei"])

    assert int(stats["locked_wei"]) == expected_locked, (
        f"locked_wei is {stats['locked_wei']} but the records account for {expected_locked}"
    )
    assert int(stats["treasury_wei"]) == expected_treasury, (
        f"treasury_wei is {stats['treasury_wei']} but gate treasuries sum to {expected_treasury}"
    )
    return expected_locked, expected_treasury


def grant_to(contract, direct_vm, applicant, gate_id=GATE_ID):
    app_id = apply_as(contract, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, contract.binding_token(gate_id, applicant))
    contract.adjudicate(app_id)
    return app_id


def deny_for(contract, direct_vm, applicant, gate_id=GATE_ID):
    app_id = apply_as(contract, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(contract.binding_token(gate_id, applicant), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    contract.adjudicate(app_id)
    return app_id


# ---------------------------------------------------------------------------
# Single flows
# ---------------------------------------------------------------------------


def test_a_fresh_registry_holds_nothing(gate):
    assert audit(gate) == (0, 0)


def test_pending_bond_is_locked_and_unearned(gate, direct_vm, direct_alice):
    apply_as(gate, direct_vm, direct_alice)
    assert audit(gate) == (BOND, 0)


def test_grant_keeps_the_bond_locked_as_a_deposit(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    assert audit(gate) == (BOND, 0)


def test_denial_moves_the_bond_to_the_treasury(gate, direct_vm, direct_alice):
    deny_for(gate, direct_vm, direct_alice)
    assert audit(gate) == (0, BOND)


def test_release_returns_the_deposit(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    assert audit(gate) == (0, 0)


def test_revocation_returns_the_deposit(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")
    assert audit(gate) == (0, 0)


def test_stale_reclaim_returns_the_bond(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-02-01T12:00:00Z")
    direct_vm.sender = direct_alice
    gate.claim_stale_application(app_id)
    assert audit(gate) == (0, 0)


def test_superseded_application_returns_the_bond(gate, direct_vm, direct_alice, direct_owner):
    from conftest import CONDITIONS, DISQUALIFIERS, POLICY_TEXT

    app_id = apply_as(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.update_policy(
        GATE_ID, POLICY_TEXT + " Revised wording for a new version.",
        json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
    )
    gate.adjudicate(app_id)
    assert audit(gate) == (0, 0)


def test_open_challenge_locks_the_stake(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0
    assert audit(gate) == (BOND + CHALLENGE_STAKE, 0)


def test_upheld_challenge_settles_both_sides(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        challenge_id = gate.challenge_access(
            GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(gate.binding_token(GATE_ID, direct_alice), disciplined=True), url=EVIDENCE_URL)
    serve_page(direct_vm, "Disciplinary notice: suspension recorded 2019.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings(disciplinary=True))
    gate.resolve_challenge(challenge_id)

    assert audit(gate) == (0, BOND - BOND // 2)


def test_rejected_challenge_settles_only_the_stake(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        challenge_id = gate.challenge_access(
            GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(gate.binding_token(GATE_ID, direct_alice)), url=EVIDENCE_URL)
    serve_page(direct_vm, "No disciplinary notices are on file for this licensee.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings())
    gate.resolve_challenge(challenge_id)

    assert audit(gate) == (BOND, CHALLENGE_STAKE - CHALLENGE_STAKE // 2)


# ---------------------------------------------------------------------------
# Treasury withdrawal
# ---------------------------------------------------------------------------


def test_treasury_withdrawal_reduces_both_totals(gate, direct_vm, direct_alice, direct_owner):
    deny_for(gate, direct_vm, direct_alice)
    audit(gate)

    direct_vm.sender = direct_owner
    remaining = gate.withdraw_treasury(GATE_ID, BOND // 4)
    assert int(remaining) == BOND - BOND // 4
    assert audit(gate) == (0, BOND - BOND // 4)


def test_treasury_can_be_fully_drained(gate, direct_vm, direct_alice, direct_owner):
    deny_for(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    assert int(gate.withdraw_treasury(GATE_ID, BOND)) == 0
    assert audit(gate) == (0, 0)


def test_treasury_withdrawal_cannot_reach_locked_funds(gate, direct_vm, direct_alice, direct_bob, direct_owner):
    """
    The solvency guarantee in one test. A gate holding a live deposit and a pending bond
    has a zero treasury, and the owner cannot withdraw a single wei of the money that
    belongs to applicants and holders.
    """
    grant_to(gate, direct_vm, direct_alice)
    apply_as(gate, direct_vm, direct_bob)
    locked, treasury = audit(gate)
    assert locked == BOND * 2
    assert treasury == 0

    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("exceeds gate treasury"):
        gate.withdraw_treasury(GATE_ID, 1)
    assert audit(gate) == (BOND * 2, 0)


def test_one_gates_treasury_is_not_reachable_from_another(
    registry, direct_vm, direct_owner, direct_alice, direct_bob, funded
):
    """
    Gate treasuries are separate balances. A second gate owner cannot draw on revenue
    earned by the first, even though both live in one contract.
    """
    register_default_gate(registry, direct_vm, direct_owner, gate_id="gate-a")
    register_default_gate(registry, direct_vm, direct_bob, gate_id="gate-b")

    deny_for(registry, direct_vm, direct_alice, gate_id="gate-a")
    assert int(json.loads(registry.get_gate("gate-a"))["treasury_wei"]) == BOND
    assert int(json.loads(registry.get_gate("gate-b"))["treasury_wei"]) == 0

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("exceeds gate treasury"):
        registry.withdraw_treasury("gate-b", 1)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        registry.withdraw_treasury("gate-a", 1)

    audit(registry)


# ---------------------------------------------------------------------------
# A long mixed scenario
# ---------------------------------------------------------------------------


def test_invariant_holds_through_a_long_mixed_scenario(
    registry, direct_vm, direct_owner, direct_accounts, funded
):
    """
    Every path the contract has, interleaved across two gates and six addresses, with
    the accounting audited after each step. Grants, denials, a release, an owner
    revocation, a stale reclaim, a policy bump that supersedes a pending application and
    invalidates a live one, an upheld challenge, a rejected challenge, and a treasury
    withdrawal.
    """
    from conftest import CONDITIONS, DISQUALIFIERS, POLICY_TEXT

    for account in direct_accounts:
        direct_vm.deal(account, 10**19)

    alice, bob, carol, dave, erin, frank = direct_accounts[:6]

    register_default_gate(registry, direct_vm, direct_owner, gate_id="gate-one")
    # gate-two runs a long lived credential, because the scenario spans a fortnight and
    # its holders need to survive the stale reclaim window to be challenged later.
    register_default_gate(
        registry, direct_vm, direct_owner, gate_id="gate-two", ttl=315360000
    )
    audit(registry)

    # Two grants on gate-one, one denial.
    grant_to(registry, direct_vm, alice, gate_id="gate-one")
    audit(registry)
    grant_to(registry, direct_vm, bob, gate_id="gate-one")
    audit(registry)
    deny_for(registry, direct_vm, carol, gate_id="gate-one")
    locked, treasury = audit(registry)
    assert locked == BOND * 2
    assert treasury == BOND

    # A grant on the second gate, so the two gates hold funds simultaneously.
    grant_to(registry, direct_vm, alice, gate_id="gate-two")
    audit(registry)

    # A holder walks away voluntarily.
    direct_vm.sender = alice
    registry.release_access("gate-one")
    audit(registry)

    # The owner revokes the other holder on out of band grounds.
    direct_vm.sender = direct_owner
    registry.revoke_access("gate-one", bob, "Licence suspended by the issuing board")
    locked, treasury = audit(registry)
    assert locked == BOND, "only the gate-two deposit remains"

    # An application nobody adjudicates, reclaimed after the window.
    stale = apply_as(registry, direct_vm, dave, gate_id="gate-one")
    audit(registry)
    warp_time(direct_vm, "2026-02-01T12:00:00Z")
    direct_vm.sender = dave
    registry.claim_stale_application(stale)
    audit(registry)

    # A pending application is caught by a policy change and refunded.
    pending = apply_as(registry, direct_vm, erin, gate_id="gate-one")
    audit(registry)
    direct_vm.sender = direct_owner
    registry.update_policy(
        "gate-one", POLICY_TEXT + " Revised wording for a new version.",
        json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
    )
    registry.adjudicate(pending)
    audit(registry)
    assert json.loads(registry.get_application(pending))["status"] == "SUPERSEDED"

    # The gate-two holder is challenged and the challenge is upheld.
    direct_vm.sender = frank
    direct_vm.value = CHALLENGE_STAKE
    try:
        upheld = registry.challenge_access(
            "gate-two", alice, REASON, json.dumps([CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0
    audit(registry)

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(registry.binding_token("gate-two", alice), disciplined=True), url=EVIDENCE_URL)
    serve_page(direct_vm, "Disciplinary notice: suspension recorded 2019.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings(disciplinary=True))
    registry.resolve_challenge(upheld)
    locked, treasury = audit(registry)
    assert locked == 0, "every bond, deposit and stake is settled"

    # A fresh grant, then a challenge that fails.
    grant_to(registry, direct_vm, carol, gate_id="gate-two")
    audit(registry)
    direct_vm.sender = frank
    direct_vm.value = CHALLENGE_STAKE
    try:
        rejected = registry.challenge_access(
            "gate-two", carol, REASON, json.dumps([CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0
    audit(registry)

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(registry.binding_token("gate-two", carol)), url=EVIDENCE_URL)
    serve_page(direct_vm, "No disciplinary notices are on file for this licensee.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings())
    registry.resolve_challenge(rejected)
    locked, _treasury = audit(registry)
    assert locked == BOND, "carol keeps her deposit and her access"
    assert registry.is_approved("gate-two", carol) is True

    # A holder lets access lapse and is granted again without releasing first. The
    # deposit behind the replaced record has to be refunded in that same transaction.
    warp_time(direct_vm, "2026-02-20T12:00:00Z")
    grant_to(registry, direct_vm, dave, gate_id="gate-one")
    assert registry.is_approved("gate-one", dave) is True
    assert json.loads(registry.get_access_history("gate-one", dave, 0, 10))["total"] == 0
    audit(registry)

    warp_time(direct_vm, "2026-03-05T12:00:00Z")  # well past the one day gate-one ttl
    assert registry.is_approved("gate-one", dave) is False, "the grant lapsed on its own"
    grant_to(registry, direct_vm, dave, gate_id="gate-one")
    archived = json.loads(registry.get_access_history("gate-one", dave, 0, 10))
    assert archived["total"] == 1, "the replaced record was archived, not overwritten"
    assert archived["records"][0]["status"] == "RENEWED"
    assert archived["records"][0]["deposit_wei"] == "0", "its deposit was refunded"
    audit(registry)

    # An eligibility rule change invalidates the renewed grant, and the holder takes
    # their collateral back rather than losing it to a rule they never agreed to.
    direct_vm.sender = direct_owner
    registry.update_gate_config(
        "gate-one", json.dumps(["registry.example.com", "boards.example.org"]),
        "raw", False, True, TTL, BOND, CHALLENGE_STAKE, COOLDOWN,
    )
    assert registry.is_approved("gate-one", dave) is False
    audit(registry)
    direct_vm.sender = dave
    assert int(registry.release_access("gate-one")) == BOND
    audit(registry)

    # Owners drain what they earned, and the locked funds are untouched.
    for gate_id in ("gate-one", "gate-two"):
        owned = int(json.loads(registry.get_gate(gate_id))["treasury_wei"])
        if owned:
            direct_vm.sender = direct_owner
            registry.withdraw_treasury(gate_id, owned)
        audit(registry)

    final_locked, final_treasury = audit(registry)
    assert final_treasury == 0
    assert final_locked == BOND
