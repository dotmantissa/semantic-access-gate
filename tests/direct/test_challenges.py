"""
Challenges: the permissionless correction path.

Nobody has to trust that an adjudication stays correct forever, and nobody has to
trust a gate owner to police their own gate. Anyone who can point at public evidence
that a holder no longer satisfies the policy can stake money on that claim and force
a fresh consensus round.

The stakes are arranged so a challenge is profitable only when it is right: a correct
challenger recovers their stake plus half the holder's collateral, and an incorrect one
loses their stake, half of it compensating the holder they troubled.
"""

import json

import pytest

from conftest import (
    BOND,
    CHALLENGE_STAKE,
    EVIDENCE_URL,
    GATE_ID,
    SECOND_URL,
    apply_as,
    grant_everything,
    hex_of,
    licence_page,
    model_findings,
    register_default_gate,
    serve_model,
    serve_page,
    warp_time,
)

CHALLENGE_URL = "https://boards.example.org/discipline/884213"
REASON = "Board record now shows a suspension against this licence"


def grant_to(gate, direct_vm, applicant):
    app_id = apply_as(gate, direct_vm, applicant)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(GATE_ID, applicant))
    gate.adjudicate(app_id)
    assert gate.is_approved(GATE_ID, applicant) is True
    return app_id


def file_challenge(gate, direct_vm, challenger, holder, urls=None, reason=REASON):
    direct_vm.sender = challenger
    direct_vm.value = CHALLENGE_STAKE
    try:
        return gate.challenge_access(
            GATE_ID, holder, reason, json.dumps(urls or [CHALLENGE_URL])
        )
    finally:
        direct_vm.value = 0


def stage_upheld(direct_vm, token):
    """Evidence that now fails the policy, so the re-adjudication denies."""
    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(token, disciplined=True), url=EVIDENCE_URL)
    serve_page(direct_vm, "Disciplinary notice: suspension recorded 2019.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings(disciplinary=True))


def stage_rejected(direct_vm, token):
    """Evidence that still satisfies the policy, so the re-adjudication grants."""
    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(token), url=EVIDENCE_URL)
    serve_page(direct_vm, "No disciplinary notices are on file for this licensee.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings())


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


def test_challenge_records_every_field(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    assert challenge_id == "chal_0"

    challenge = json.loads(gate.get_challenge(challenge_id))
    assert challenge["gate_id"] == GATE_ID
    assert challenge["holder"].lower() == hex_of(direct_alice)
    assert challenge["challenger"].lower() == hex_of(direct_bob)
    assert challenge["stake_wei"] == str(CHALLENGE_STAKE)
    assert challenge["status"] == "OPEN"
    assert challenge["reason"] == REASON
    assert challenge["evidence_urls"] == [CHALLENGE_URL]
    assert challenge["outcome_decision"] == ""


def test_stake_is_escrowed_on_filing(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    before = int(json.loads(gate.get_registry_stats())["locked_wei"])
    file_challenge(gate, direct_vm, direct_bob, direct_alice)
    after = int(json.loads(gate.get_registry_stats())["locked_wei"])
    assert after == before + CHALLENGE_STAKE


def test_exact_stake_is_required(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE - 1
    try:
        with direct_vm.expect_revert("Stake must be exactly"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_challenge_marks_the_record_and_counts(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    file_challenge(gate, direct_vm, direct_bob, direct_alice)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["challenge_count"] == 1
    assert json.loads(gate.access_status(GATE_ID, direct_alice))["has_open_challenge"] is True


def test_a_challenged_record_stays_approved_until_resolved(gate, direct_vm, direct_alice, direct_bob):
    """
    Filing a challenge is an accusation, not a finding. Suspending access on an
    unproven claim would make challenges a cheap denial of service against holders.
    """
    grant_to(gate, direct_vm, direct_alice)
    file_challenge(gate, direct_vm, direct_bob, direct_alice)
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_a_holder_cannot_challenge_themselves(gate, direct_vm, direct_alice):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("cannot challenge their own"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_only_one_challenge_may_be_open_at_a_time(gate, direct_vm, direct_alice, direct_bob, direct_charlie):
    grant_to(gate, direct_vm, direct_alice)
    file_challenge(gate, direct_vm, direct_bob, direct_alice)
    direct_vm.sender = direct_charlie
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("already open"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_a_record_that_is_not_live_cannot_be_challenged(gate, direct_vm, direct_alice, direct_bob):
    """Nothing to correct: an expired record already grants nothing."""
    grant_to(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-01-20T12:00:00Z")
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("not live"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_a_policy_invalidated_record_cannot_be_challenged(
    gate, direct_vm, direct_alice, direct_bob, direct_owner
):
    from conftest import CONDITIONS, DISQUALIFIERS, POLICY_TEXT

    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.update_policy(
        GATE_ID, POLICY_TEXT + " Additional wording to force a new version.",
        json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
    )
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("not live"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_challenging_a_missing_record_is_rejected(gate, direct_vm, direct_bob, direct_charlie):
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("No access record"):
            gate.challenge_access(GATE_ID, direct_charlie, REASON, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


@pytest.mark.parametrize("reason", ["short", "x" * 501])
def test_challenge_reason_length_is_enforced(gate, direct_vm, direct_alice, direct_bob, reason):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("reason must be"):
            gate.challenge_access(GATE_ID, direct_alice, reason, json.dumps([CHALLENGE_URL]))
    finally:
        direct_vm.value = 0


def test_challenge_evidence_must_pass_the_host_allowlist(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("Host not permitted"):
            gate.challenge_access(
                GATE_ID, direct_alice, REASON, json.dumps(["https://rumours.example.net/x"])
            )
    finally:
        direct_vm.value = 0


def test_challenge_evidence_count_is_capped(gate, direct_vm, direct_alice, direct_bob):
    """
    A challenger adds to the holder's evidence rather than replacing it, so the cap is
    tighter: the combined prompt still has to fit within a reliable response budget.
    """
    grant_to(gate, direct_vm, direct_alice)
    urls = [f"https://boards.example.org/{i}" for i in range(3)]
    direct_vm.sender = direct_bob
    direct_vm.value = CHALLENGE_STAKE
    try:
        with direct_vm.expect_revert("between 1 and"):
            gate.challenge_access(GATE_ID, direct_alice, REASON, json.dumps(urls))
    finally:
        direct_vm.value = 0


def test_an_open_challenge_blocks_release(gate, direct_vm, direct_alice, direct_bob):
    """A holder must not be able to walk off with the collateral mid challenge."""
    grant_to(gate, direct_vm, direct_alice)
    file_challenge(gate, direct_vm, direct_bob, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("challenge is open"):
        gate.release_access(GATE_ID)


def test_an_open_challenge_blocks_owner_revocation(gate, direct_vm, direct_alice, direct_bob, direct_owner):
    """
    Otherwise a gate owner could rescue an ally by revoking first, returning the deposit
    and leaving the challenger's stake with nothing to win.
    """
    grant_to(gate, direct_vm, direct_alice)
    file_challenge(gate, direct_vm, direct_bob, direct_alice)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("challenge is open"):
        gate.revoke_access(GATE_ID, direct_alice, "stepping in before resolution")


# ---------------------------------------------------------------------------
# Resolution: upheld
# ---------------------------------------------------------------------------


def test_upheld_challenge_revokes_access(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    assert result["status"] == "UPHELD"
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "DISQUALIFIER_PRESENT"
    assert result["access_status"] == "REVOKED"
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_upheld_challenge_pays_the_challenger_and_the_treasury(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    challenger_share = BOND // 2
    assert int(result["challenger_payout_wei"]) == CHALLENGE_STAKE + challenger_share
    assert int(result["holder_payout_wei"]) == 0
    assert int(result["treasury_credit_wei"]) == BOND - challenger_share

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == 0, "both the stake and the deposit are settled"
    assert int(stats["treasury_wei"]) == BOND - challenger_share


def test_upheld_challenge_conserves_value(gate, direct_vm, direct_alice, direct_bob):
    """Nothing is minted or lost: stake plus deposit equals every payout combined."""
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    total_out = (
        int(result["challenger_payout_wei"])
        + int(result["holder_payout_wei"])
        + int(result["treasury_credit_wei"])
    )
    assert total_out == CHALLENGE_STAKE + BOND


def test_upheld_challenge_zeroes_the_deposit_and_counts_the_revocation(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(challenge_id)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["deposit_wei"] == "0"
    assert record["status"] == "REVOKED"
    assert challenge_id in record["close_reason"]
    assert json.loads(gate.gate_stats(GATE_ID))["total_revoked"] == 1


def test_a_slashed_holder_faces_the_cooldown(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(challenge_id)

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is False
    assert pre["reason"] == "COOLDOWN_ACTIVE"


def test_challenger_evidence_reaches_the_adjudication(gate, direct_vm, direct_alice, direct_bob):
    """
    The challenger's documents are fetched alongside the holder's, which is what lets a
    challenge introduce a fact the original evidence never mentioned.
    """
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_upheld(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    gate.resolve_challenge(challenge_id)
    app = json.loads(gate.get_application(json.loads(gate.get_access_record(GATE_ID, direct_alice))["application_id"]))
    assert app["evidence_urls"] == [EVIDENCE_URL], "the original application is unchanged"

    challenge = json.loads(gate.get_challenge(challenge_id))
    assert challenge["evidence_urls"] == [CHALLENGE_URL]
    assert challenge["outcome_decision"] == "DENIED"


# ---------------------------------------------------------------------------
# Resolution: rejected
# ---------------------------------------------------------------------------


def test_rejected_challenge_leaves_access_standing(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    assert result["status"] == "REJECTED"
    assert result["decision"] == "GRANTED"
    assert result["access_status"] == "ACTIVE"
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_rejected_challenge_slashes_the_stake_and_pays_the_holder(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    holder_share = CHALLENGE_STAKE // 2
    assert int(result["challenger_payout_wei"]) == 0
    assert int(result["holder_payout_wei"]) == holder_share
    assert int(result["treasury_credit_wei"]) == CHALLENGE_STAKE - holder_share

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == BOND, "the holder's deposit stays behind the record"
    assert int(stats["treasury_wei"]) == CHALLENGE_STAKE - holder_share


def test_rejected_challenge_conserves_value(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))

    result = json.loads(gate.resolve_challenge(challenge_id))
    total_out = (
        int(result["challenger_payout_wei"])
        + int(result["holder_payout_wei"])
        + int(result["treasury_credit_wei"])
    )
    assert total_out == CHALLENGE_STAKE


def test_holder_keeps_their_deposit_after_a_rejected_challenge(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(challenge_id)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["deposit_wei"] == str(BOND)
    assert record["status"] == "ACTIVE"


def test_release_is_possible_once_a_challenge_is_resolved(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(challenge_id)

    assert json.loads(gate.access_status(GATE_ID, direct_alice))["has_open_challenge"] is False
    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND


def test_a_second_challenge_is_possible_after_the_first_is_rejected(
    gate, direct_vm, direct_alice, direct_bob, direct_charlie
):
    grant_to(gate, direct_vm, direct_alice)
    first = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(first)

    second = file_challenge(gate, direct_vm, direct_charlie, direct_alice)
    assert second != first
    assert json.loads(gate.get_access_record(GATE_ID, direct_alice))["challenge_count"] == 2


# ---------------------------------------------------------------------------
# Resolution mechanics
# ---------------------------------------------------------------------------


def test_anyone_may_resolve_an_open_challenge(gate, direct_vm, direct_alice, direct_bob, direct_charlie):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    direct_vm.sender = direct_charlie
    assert json.loads(gate.resolve_challenge(challenge_id))["status"] == "REJECTED"


def test_resolving_twice_is_rejected(gate, direct_vm, direct_alice, direct_bob):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    stage_rejected(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    gate.resolve_challenge(challenge_id)
    with direct_vm.expect_revert("already"):
        gate.resolve_challenge(challenge_id)


def test_resolving_an_unknown_challenge_is_rejected(gate, direct_vm):
    with direct_vm.expect_revert("Unknown challenge"):
        gate.resolve_challenge("chal_404")


def test_unknown_challenge_reads_empty(gate):
    assert gate.get_challenge("chal_404") == ""


def test_resolution_reverts_cleanly_on_a_transient_fault(gate, direct_vm, direct_alice, direct_bob):
    """
    An outage during resolution must leave the challenge open and every stake intact,
    so it can simply be resolved again later.
    """
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = file_challenge(gate, direct_vm, direct_bob, direct_alice)
    direct_vm.clear_mocks()
    serve_page(direct_vm, "", url=EVIDENCE_URL, status=503)

    with direct_vm.expect_revert("TRANSIENT"):
        gate.resolve_challenge(challenge_id)

    assert json.loads(gate.get_challenge(challenge_id))["status"] == "OPEN"
    assert int(json.loads(gate.get_registry_stats())["locked_wei"]) == BOND + CHALLENGE_STAKE
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_a_zero_stake_gate_still_resolves(registry, direct_vm, direct_owner, direct_alice, direct_bob, funded):
    """
    Gates may set both the bond and the stake to zero when the policy is public good
    rather than adversarial. The settlement arithmetic still has to hold at zero.
    """
    register_default_gate(registry, direct_vm, direct_owner, gate_id="free-gate", bond=0, stake=0)
    app_id = apply_as(registry, direct_vm, direct_alice, gate_id="free-gate", bond=0)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, registry.binding_token("free-gate", direct_alice))
    registry.adjudicate(app_id)
    assert registry.is_approved("free-gate", direct_alice) is True

    direct_vm.sender = direct_bob
    direct_vm.value = 0
    challenge_id = registry.challenge_access(
        "free-gate", direct_alice, REASON, json.dumps([CHALLENGE_URL])
    )
    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page(registry.binding_token("free-gate", direct_alice), disciplined=True), url=EVIDENCE_URL)
    serve_page(direct_vm, "Disciplinary notice: suspension recorded 2019.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings(disciplinary=True))

    result = json.loads(registry.resolve_challenge(challenge_id))
    assert result["status"] == "UPHELD"
    assert int(result["challenger_payout_wei"]) == 0
    assert int(result["treasury_credit_wei"]) == 0
    assert int(json.loads(registry.get_registry_stats())["locked_wei"]) == 0
