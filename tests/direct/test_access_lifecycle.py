"""
The life of an access record after it is issued: expiry, voluntary release, and
owner revocation.

Records are deliberately time limited. A credential that was real a year ago is not
evidence that it is real now, so access lapses on its own and has to be re-established
against the policy rather than persisting until someone remembers to remove it.
"""

import json

import pytest

from conftest import (
    BOND,
    EVIDENCE_URL,
    GATE_ID,
    TTL,
    apply_as,
    grant_everything,
    hex_of,
    register_default_gate,
    warp_time,
)


def grant_to(gate, direct_vm, applicant, gate_id=GATE_ID):
    app_id = apply_as(gate, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(gate_id, applicant))
    gate.adjudicate(app_id)
    return app_id


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_access_lapses_when_the_ttl_elapses(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    assert gate.is_approved(GATE_ID, direct_alice) is True

    warp_time(direct_vm, "2026-01-16T12:00:01Z")  # one second past a one day ttl
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_access_still_holds_just_before_expiry(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-16T11:59:59Z")
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_expiry_is_reported_with_its_own_reason(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-20T12:00:00Z")

    status = json.loads(gate.access_status(GATE_ID, direct_alice))
    assert status["approved"] is False
    assert status["reason"] == "EXPIRED"
    assert status["seconds_remaining"] == 0
    assert status["record_policy_version"] == status["current_policy_version"]


def test_remaining_time_counts_down(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    assert json.loads(gate.access_status(GATE_ID, direct_alice))["seconds_remaining"] == TTL

    warp_time(direct_vm, "2026-01-15T18:00:00Z")  # six hours in
    assert json.loads(gate.access_status(GATE_ID, direct_alice))["seconds_remaining"] == TTL - 21600


def test_an_expired_record_keeps_its_stored_status(gate, direct_vm, direct_alice):
    """
    Expiry is computed, not written. Nothing has to run at the moment a record lapses,
    which is what lets one view answer the question for any number of holders.
    """
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-20T12:00:00Z")

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "ACTIVE"
    assert record["closed_at"] == 0
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_expired_holder_drops_out_of_the_live_tally(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    assert json.loads(gate.gate_stats(GATE_ID))["live_holders"] == 1

    warp_time(direct_vm, "2026-01-20T12:00:00Z")
    stats = json.loads(gate.gate_stats(GATE_ID))
    assert stats["live_holders"] == 0
    assert stats["ever_granted_holders"] == 1
    assert stats["total_granted"] == 1


def test_expired_holder_may_apply_again(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-20T12:00:00Z")

    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["eligible"] is True
    second = apply_as(gate, direct_vm, direct_alice)
    assert json.loads(gate.get_application(second))["status"] == "PENDING"


def test_expired_holder_can_still_reclaim_their_deposit(gate, direct_vm, direct_alice):
    """A lapsed record is not a forfeit. The collateral returns to an honest holder."""
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-20T12:00:00Z")

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND
    assert json.loads(gate.get_access_record(GATE_ID, direct_alice))["status"] == "RELEASED"


def test_a_long_lived_gate_can_be_configured(registry, direct_vm, direct_owner, direct_alice, funded):
    register_default_gate(registry, direct_vm, direct_owner, gate_id="long-gate", ttl=315360000)
    grant_to(registry, direct_vm, direct_alice, gate_id="long-gate")
    warp_time(direct_vm, "2030-01-01T00:00:00Z")
    assert registry.is_approved("long-gate", direct_alice) is True


def test_a_short_lived_gate_expires_quickly(registry, direct_vm, direct_owner, direct_alice, funded):
    register_default_gate(registry, direct_vm, direct_owner, gate_id="short-gate", ttl=60)
    grant_to(registry, direct_vm, direct_alice, gate_id="short-gate")
    assert registry.is_approved("short-gate", direct_alice) is True

    warp_time(direct_vm, "2026-01-15T12:01:01Z")
    assert registry.is_approved("short-gate", direct_alice) is False


# ---------------------------------------------------------------------------
# Voluntary release
# ---------------------------------------------------------------------------


def test_release_returns_the_deposit_and_closes_the_record(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    locked_before = int(json.loads(gate.get_registry_stats())["locked_wei"])

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "RELEASED"
    assert record["deposit_wei"] == "0"
    assert record["closed_at"] > 0
    assert "Released by holder" in record["close_reason"]

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == locked_before - BOND
    assert stats["treasury_wei"] == "0"
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_release_reports_its_own_reason(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    assert json.loads(gate.access_status(GATE_ID, direct_alice))["reason"] == "RELEASED"


def test_release_requires_an_existing_record(gate, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("No access record"):
        gate.release_access(GATE_ID)


def test_release_cannot_be_repeated(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)
    with direct_vm.expect_revert("already"):
        gate.release_access(GATE_ID)


def test_release_on_an_unknown_gate_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown gate"):
        gate.release_access("no-such-gate")


def test_released_holder_may_apply_again(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)

    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["eligible"] is True
    assert apply_as(gate, direct_vm, direct_alice)


def test_one_holder_releasing_does_not_touch_another(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    grant_to(gate, direct_vm, direct_bob)

    direct_vm.sender = direct_alice
    gate.release_access(GATE_ID)

    assert gate.is_approved(GATE_ID, direct_alice) is False
    assert gate.is_approved(GATE_ID, direct_bob) is True
    assert int(json.loads(gate.get_registry_stats())["locked_wei"]) == BOND


# ---------------------------------------------------------------------------
# Owner revocation
# ---------------------------------------------------------------------------


def test_owner_can_revoke_and_the_deposit_is_returned(gate, direct_vm, direct_alice, direct_owner):
    """
    Revocation covers facts the policy cannot see yet, such as a licence suspended
    between adjudications. It is not a finding that the applicant lied, so the deposit
    goes back. Punishing fraud is what challenge_access is for.
    """
    grant_to(gate, direct_vm, direct_alice)
    locked_before = int(json.loads(gate.get_registry_stats())["locked_wei"])

    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "REVOKED"
    assert record["deposit_wei"] == "0"
    assert "Licence suspended" in record["close_reason"]
    assert gate.is_approved(GATE_ID, direct_alice) is False

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == locked_before - BOND
    assert stats["treasury_wei"] == "0", "revocation is not a slashing"


def test_revocation_is_reported_with_its_own_reason(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")
    assert json.loads(gate.access_status(GATE_ID, direct_alice))["reason"] == "REVOKED"


def test_revocation_increments_the_gate_counter(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")
    assert json.loads(gate.gate_stats(GATE_ID))["total_revoked"] == 1


def test_only_the_owner_can_revoke(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        gate.revoke_access(GATE_ID, direct_alice, "I do not like this person")


def test_a_holder_cannot_revoke_themselves_through_the_owner_path(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only the gate owner"):
        gate.revoke_access(GATE_ID, direct_alice, "releasing my own access")


def test_revoking_a_missing_record_is_rejected(gate, direct_vm, direct_owner, direct_bob):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("No access record"):
        gate.revoke_access(GATE_ID, direct_bob, "never had access to begin with")


def test_revocation_cannot_be_repeated(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")
    with direct_vm.expect_revert("already"):
        gate.revoke_access(GATE_ID, direct_alice, "again")


def test_a_revoked_holder_may_reapply(gate, direct_vm, direct_alice, direct_owner):
    """
    Revocation is not a ban. If the holder can establish the policy again, the evidence
    decides, not the owner's memory of having removed them.
    """
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")

    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["eligible"] is True
    app_id = apply_as(gate, direct_vm, direct_alice)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    assert json.loads(gate.adjudicate(app_id))["decision"] == "GRANTED"
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_revocation_does_not_affect_other_holders(gate, direct_vm, direct_alice, direct_bob, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    grant_to(gate, direct_vm, direct_bob)

    direct_vm.sender = direct_owner
    gate.revoke_access(GATE_ID, direct_alice, "Licence suspended by the issuing board")

    assert gate.is_approved(GATE_ID, direct_alice) is False
    assert gate.is_approved(GATE_ID, direct_bob) is True


# ---------------------------------------------------------------------------
# Absent records
# ---------------------------------------------------------------------------


def test_unknown_subject_is_not_approved(gate, direct_charlie):
    assert gate.is_approved(GATE_ID, direct_charlie) is False
    status = json.loads(gate.access_status(GATE_ID, direct_charlie))
    assert status["approved"] is False
    assert status["reason"] == "NO_RECORD"


def test_unknown_gate_is_never_approved(gate, direct_alice):
    """
    A consumer pointed at a gate that does not exist must fail closed, not open.
    """
    assert gate.is_approved("no-such-gate", direct_alice) is False
    status = json.loads(gate.access_status("no-such-gate", direct_alice))
    assert status["approved"] is False
    assert status["reason"] == "UNKNOWN_GATE"


def test_access_record_read_for_a_missing_record_is_empty(gate, direct_charlie):
    assert gate.get_access_record(GATE_ID, direct_charlie) == ""


def test_a_grant_on_one_gate_does_not_leak_to_another(
    gate, direct_vm, direct_owner, direct_alice
):
    """
    Records are keyed by gate and holder together, so approval is never ambient. This
    is why the gate id charset excludes the key separator.
    """
    register_default_gate(gate, direct_vm, direct_owner, gate_id="other-gate")
    grant_to(gate, direct_vm, direct_alice)

    assert gate.is_approved(GATE_ID, direct_alice) is True
    assert gate.is_approved("other-gate", direct_alice) is False
