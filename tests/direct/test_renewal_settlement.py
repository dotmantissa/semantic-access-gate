"""
Renewal settlement: a new grant must never strand the deposit behind the record it
replaces.

An access record lives at one slot per (gate, holder). A holder whose record has
lapsed - because it expired, or because a policy or rules change invalidated it -
is allowed to apply again, and a successful re-adjudication has to write into that
same slot. The record being replaced is still ACTIVE and is still carrying the
deposit that backed it.

Writing over it would destroy the holder's collateral: the wei would remain inside
total_locked_wei with no record left pointing at it and no method able to reach it,
so the registry would permanently claim to be holding money it could never pay out.
The contract instead closes the prior record, archives it, and refunds its deposit
in the same transaction, reducing total_locked_wei by exactly the amount refunded.

The decisive assertion in this file is not that a counter looks right. It is that
after any sequence of renewals the registry can still be drained to zero, which is
only true if nothing was stranded along the way.
"""

import json

import pytest

from conftest import (
    BOND,
    CHALLENGE_STAKE,
    CONDITIONS,
    DISQUALIFIERS,
    EVIDENCE_URL,
    GATE_ID,
    POLICY_TEXT,
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
from test_solvency import audit

CHALLENGE_URL = "https://boards.example.org/discipline/884213"
REASON = "Board record now shows a suspension against this licence"

# Far enough past the one day TTL that every record issued at BASE_TIME has lapsed.
AFTER_EXPIRY = "2026-01-20T12:00:00Z"


def grant_to(contract, direct_vm, applicant, gate_id=GATE_ID):
    """Take one address all the way to a live grant and return the adjudication."""
    app_id = apply_as(contract, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, contract.binding_token(gate_id, applicant))
    result = json.loads(contract.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
    return result


def deny_for(contract, direct_vm, applicant, gate_id=GATE_ID):
    app_id = apply_as(contract, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    serve_page(
        direct_vm,
        licence_page(contract.binding_token(gate_id, applicant), active=False),
    )
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    return json.loads(contract.adjudicate(app_id))


def bump_policy(contract, direct_vm, owner, gate_id=GATE_ID):
    direct_vm.sender = owner
    return contract.update_policy(
        gate_id,
        POLICY_TEXT + " Revised wording that creates a new version.",
        json.dumps(CONDITIONS),
        json.dumps(DISQUALIFIERS),
    )


def bump_rules(contract, direct_vm, owner, gate_id=GATE_ID):
    """Change an adjudication-relevant setting, which bumps the rules version."""
    direct_vm.sender = owner
    return contract.update_gate_config(
        gate_id,
        json.dumps(["registry.example.com", "boards.example.org"]),
        "raw",
        False,  # binding_required flipped off: an eligibility change
        True,
        TTL,
        BOND,
        CHALLENGE_STAKE,
        3600,
    )


def locked(contract):
    return int(json.loads(contract.get_registry_stats())["locked_wei"])


# ---------------------------------------------------------------------------
# The reported defect: renewal after expiry
# ---------------------------------------------------------------------------


def test_renewal_after_expiry_refunds_the_prior_deposit(gate, direct_vm, direct_alice):
    """
    The headline case. Alice is granted, her record lapses, and she is granted again
    without ever calling release_access. The deposit behind the lapsed record must
    come back to her in the renewing transaction, not sit in the contract forever.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)

    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == BOND


def test_renewal_after_expiry_leaves_exactly_one_bond_locked(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    assert locked(gate) == BOND

    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    assert locked(gate) == BOND, "a renewal must not accumulate a second locked bond"


def test_renewal_reconciles_against_an_independent_walk_of_the_records(
    gate, direct_vm, direct_alice
):
    """
    The accounting invariant from the solvency suite, applied to the renewal path.
    audit() recomputes the registry totals from the stored records alone. A stranded
    deposit shows up here as a running total that no record accounts for.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    assert audit(gate) == (BOND, 0)


def test_every_wei_is_still_reclaimable_after_a_renewal(gate, direct_vm, direct_alice):
    """
    The decisive test. Counters can be made to agree; solvency cannot be faked. After
    a renewal the holder releases her one live record, and the registry must be
    holding nothing at all. Any deposit orphaned by the renewal would still be sitting
    in locked_wei here with no way left to reach it.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND

    assert locked(gate) == 0, "a renewal stranded funds the holder can never reclaim"
    assert audit(gate) == (0, 0)


def test_repeated_renewals_never_accumulate_locked_funds(gate, direct_vm, direct_alice):
    """Five lapse-and-renew cycles. The registry's obligation never grows past one bond."""
    grant_to(gate, direct_vm, direct_alice)
    for day in range(20, 25):
        warp_time(direct_vm, f"2026-01-{day}T12:00:00Z")
        grant_to(gate, direct_vm, direct_alice)
        assert locked(gate) == BOND
        audit(gate)

    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    assert locked(gate) == 0


# ---------------------------------------------------------------------------
# Renewal after a policy or rules supersession
# ---------------------------------------------------------------------------


def test_renewal_after_policy_supersession_refunds_the_prior_deposit(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The second route into the same slot. The record here has not expired; it was
    invalidated by a policy bump, which leaves it ACTIVE and still holding its
    deposit. Re-qualifying under the new policy must settle the old deposit.
    """
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    assert gate.is_approved(GATE_ID, direct_alice) is False

    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == BOND
    assert locked(gate) == BOND
    assert audit(gate) == (BOND, 0)


def test_renewal_after_rules_supersession_refunds_the_prior_deposit(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    bump_rules(gate, direct_vm, direct_owner)
    assert gate.is_approved(GATE_ID, direct_alice) is False

    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == BOND
    assert audit(gate) == (BOND, 0)


def test_a_superseded_holder_who_renews_can_still_drain_to_zero(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    grant_to(gate, direct_vm, direct_alice)

    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    assert audit(gate) == (0, 0)


# ---------------------------------------------------------------------------
# The record being replaced is preserved, not destroyed
# ---------------------------------------------------------------------------


def test_renewal_closes_the_prior_record_rather_than_overwriting_it(
    gate, direct_vm, direct_alice
):
    first = grant_to(gate, direct_vm, direct_alice)["application_id"]
    warp_time(direct_vm, AFTER_EXPIRY)
    second = grant_to(gate, direct_vm, direct_alice)["application_id"]

    history = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))
    assert history["total"] == 1
    archived = history["records"][0]
    assert archived["application_id"] == first
    assert archived["status"] == "RENEWED"
    assert archived["deposit_wei"] == "0", "the archived deposit was paid out"
    assert archived["closed_at"] > 0
    assert second in archived["close_reason"]

    live = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert live["application_id"] == second
    assert live["status"] == "ACTIVE"


def test_the_archived_record_keeps_the_versions_it_was_issued_under(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The archive is an audit trail, so a replaced record must still say which frame
    granted it rather than being restamped with the current one.
    """
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    grant_to(gate, direct_vm, direct_alice)

    archived = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))["records"][0]
    assert archived["policy_version"] == 1

    live = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert live["policy_version"] == 2


def test_the_renewed_record_carries_exactly_one_bond(gate, direct_vm, direct_alice):
    """
    The refund is a refund, not a carry forward into a doubled deposit. Challenge
    economics depend on a deposit equal to one bond, so the new record must hold
    exactly that.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert int(record["deposit_wei"]) == BOND


def test_history_grows_by_one_for_every_renewal(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    for index, day in enumerate((20, 21, 22), start=1):
        warp_time(direct_vm, f"2026-01-{day}T12:00:00Z")
        grant_to(gate, direct_vm, direct_alice)
        history = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))
        assert history["total"] == index
        assert all(r["status"] == "RENEWED" for r in history["records"])


def test_history_is_empty_for_a_holder_who_never_renewed(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    history = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))
    assert history["total"] == 0
    assert history["records"] == []


def test_history_records_every_grant_a_slot_has_held(gate, direct_vm, direct_alice, direct_owner):
    """
    The slot's whole sequence, however each entry ended: released, revoked, and
    replaced while still open.
    """
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)

    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")

    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    history = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))
    assert [r["status"] for r in history["records"]] == ["RELEASED", "REVOKED", "RENEWED"]
    assert all(r["deposit_wei"] == "0" for r in history["records"])
    assert audit(gate) == (BOND, 0)


def test_history_paginates(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    for day in (20, 21, 22):
        warp_time(direct_vm, f"2026-01-{day}T12:00:00Z")
        grant_to(gate, direct_vm, direct_alice)

    page = json.loads(gate.get_access_history(GATE_ID, direct_alice, 1, 1))
    assert page["total"] == 3
    assert page["offset"] == 1
    assert len(page["records"]) == 1


# ---------------------------------------------------------------------------
# Paths that must NOT trigger a second payout
# ---------------------------------------------------------------------------


def test_renewal_after_a_release_does_not_refund_twice(gate, direct_vm, direct_alice):
    """
    The deposit has already gone back to the holder, and the record is RELEASED with
    a zero deposit. The renewal must settle nothing.
    """
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    assert locked(gate) == 0

    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == 0
    assert locked(gate) == BOND
    assert audit(gate) == (BOND, 0)

    # It is still archived, and it keeps its own ending rather than being relabelled.
    archived = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))["records"]
    assert [r["status"] for r in archived] == ["RELEASED"]
    assert "Released by holder" in archived[0]["close_reason"]


def test_renewal_after_a_revocation_does_not_refund_twice(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")
    assert locked(gate) == 0

    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == 0
    assert audit(gate) == (BOND, 0)

    archived = json.loads(gate.get_access_history(GATE_ID, direct_alice, 0, 10))["records"]
    assert [r["status"] for r in archived] == ["REVOKED"]
    assert "Licence suspended" in archived[0]["close_reason"]


def test_a_first_grant_refunds_nothing(gate, direct_vm, direct_alice):
    result = grant_to(gate, direct_vm, direct_alice)
    assert int(result["refunded_prior_deposit_wei"]) == 0


def test_release_after_a_renewal_returns_only_the_live_deposit(
    gate, direct_vm, direct_alice
):
    """A holder cannot be paid twice for the record that was already settled."""
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND
    with direct_vm.expect_revert("already"):
        gate.release_access(GATE_ID)


def test_a_denied_renewal_leaves_the_prior_record_reachable(
    gate, direct_vm, direct_alice
):
    """
    Only a grant writes the slot. A denied re-application must leave the lapsed
    record exactly as it was, with its deposit still reclaimable by the holder.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)

    result = deny_for(gate, direct_vm, direct_alice)
    assert result["decision"] == "DENIED"

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "ACTIVE"
    assert int(record["deposit_wei"]) == BOND

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND
    assert locked(gate) == 0


def test_renewal_does_not_disturb_another_holder(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    grant_to(gate, direct_vm, direct_bob)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    assert gate.is_approved(GATE_ID, direct_alice) is True
    assert gate.is_approved(GATE_ID, direct_bob) is False, "bob's record lapsed on time"
    bob = json.loads(gate.get_access_record(GATE_ID, direct_bob))
    assert int(bob["deposit_wei"]) == BOND, "bob's collateral is untouched"
    assert locked(gate) == BOND * 2
    assert audit(gate) == (BOND * 2, 0)


def test_renewal_does_not_double_count_the_holder(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(gate, direct_vm, direct_alice)

    stats = json.loads(gate.gate_stats(GATE_ID))
    assert stats["ever_granted_holders"] == 1
    assert stats["live_holders"] == 1
    assert stats["total_granted"] == 2


def test_renewal_across_two_gates_is_independent(
    registry, direct_vm, direct_owner, direct_alice, funded
):
    register_default_gate(registry, direct_vm, direct_owner, gate_id="gate-a")
    register_default_gate(registry, direct_vm, direct_owner, gate_id="gate-b")

    grant_to(registry, direct_vm, direct_alice, gate_id="gate-a")
    grant_to(registry, direct_vm, direct_alice, gate_id="gate-b")
    warp_time(direct_vm, AFTER_EXPIRY)
    grant_to(registry, direct_vm, direct_alice, gate_id="gate-a")

    assert registry.is_approved("gate-a", direct_alice) is True
    assert registry.is_approved("gate-b", direct_alice) is False
    assert audit(registry) == (BOND * 2, 0)


# ---------------------------------------------------------------------------
# A challenged record cannot be renewed out from underneath its challenge
# ---------------------------------------------------------------------------


def open_challenge(contract, direct_vm, challenger, holder, gate_id=GATE_ID):
    direct_vm.sender = challenger
    direct_vm.value = CHALLENGE_STAKE
    try:
        return contract.challenge_access(
            gate_id, holder, REASON, json.dumps([CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0


def test_a_live_challenged_record_is_blocked_by_the_live_access_rule(
    registry, direct_vm, direct_owner, direct_alice, direct_bob, funded
):
    """
    While the record is still live the existing rule already refuses a second
    application, so the challenge is safe for that whole window. The new guard is
    what covers the window after the record lapses.
    """
    register_default_gate(registry, direct_vm, direct_owner, gate_id="long-gate", ttl=315360000)
    grant_to(registry, direct_vm, direct_alice, gate_id="long-gate")
    open_challenge(registry, direct_vm, direct_bob, direct_alice, gate_id="long-gate")

    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("already holds live access"):
            registry.apply_for_access("long-gate", json.dumps([EVIDENCE_URL]), "again")
    finally:
        direct_vm.value = 0


def test_can_apply_reports_the_open_challenge_on_a_lapsed_record(
    gate, direct_vm, direct_alice, direct_bob
):
    """
    can_apply must name the same first blocking reason apply_for_access raises on, so
    a front end never explains a refusal differently from the chain.
    """
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is False
    assert pre["reason"] == "CHALLENGE_OPEN"
    assert pre["open_challenge_id"] == challenge_id


def test_an_expired_challenged_record_still_cannot_be_renewed(
    gate, direct_vm, direct_alice, direct_bob
):
    """
    The challenge was opened while the record was live; the record then expired. The
    block has to survive that, because this is exactly the sequence that would let a
    renewal replace a record a stake is riding on.
    """
    grant_to(gate, direct_vm, direct_alice)
    open_challenge(gate, direct_vm, direct_bob, direct_alice)
    warp_time(direct_vm, AFTER_EXPIRY)

    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("challenge is open"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "again")
    finally:
        direct_vm.value = 0
    assert audit(gate) == (BOND + CHALLENGE_STAKE, 0)
