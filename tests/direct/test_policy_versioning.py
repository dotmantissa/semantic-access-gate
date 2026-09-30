"""
Policy versioning and automatic invalidation.

The claim under test: when the policy changes, every access record issued under the
old text stops counting as approved immediately, with no loop over holders and no
migration. The mechanism is a single integer compared on read.

This is what makes the primitive safe to build an ecosystem on. A consumer contract
that defers to a gate inherits policy changes without redeploying, and a gate owner
cannot quietly tighten a policy while old grants keep working.
"""

import json

import pytest

from conftest import (
    BOND,
    CONDITIONS,
    DISQUALIFIERS,
    EVIDENCE_URL,
    GATE_ID,
    POLICY_TEXT,
    apply_as,
    grant_everything,
    hex_of,
    licence_page,
    model_findings,
    serve_model,
    serve_page,
)

TIGHTER_POLICY = (
    "Access is limited to individuals who hold a currently active licence to practise "
    "medicine, who are board certified in a recognised clinical specialty, who have no "
    "disciplinary action recorded, and who additionally hold current hospital admitting "
    "privileges at an accredited institution."
)

TIGHTER_CONDITIONS = CONDITIONS + [
    {
        "id": "c3_admitting",
        "text": "The evidence shows current hospital admitting privileges at an accredited institution",
    }
]


def grant_to(gate, direct_vm, applicant):
    """
    Take one address all the way to a live grant.

    Mocks are cleared first because the harness matches the earliest registered
    pattern, so a page left over from a previous holder would be served here and its
    binding token would not match this applicant.
    """
    app_id = apply_as(gate, direct_vm, applicant)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(GATE_ID, applicant))
    gate.adjudicate(app_id)
    assert gate.is_approved(GATE_ID, applicant) is True
    return app_id


def bump_policy(gate, direct_vm, owner, policy=TIGHTER_POLICY, conditions=None):
    direct_vm.sender = owner
    return gate.update_policy(
        GATE_ID,
        policy,
        json.dumps(TIGHTER_CONDITIONS if conditions is None else conditions),
        json.dumps(DISQUALIFIERS),
    )


# ---------------------------------------------------------------------------
# Versioning mechanics
# ---------------------------------------------------------------------------


def test_policy_update_bumps_the_version(gate, direct_vm, direct_owner):
    assert json.loads(gate.get_policy(GATE_ID))["policy_version"] == 1
    new_version = bump_policy(gate, direct_vm, direct_owner)
    assert int(new_version) == 2
    assert json.loads(gate.get_policy(GATE_ID))["policy_version"] == 2


def test_policy_update_replaces_text_and_conditions(gate, direct_vm, direct_owner):
    bump_policy(gate, direct_vm, direct_owner)
    policy = json.loads(gate.get_policy(GATE_ID))
    assert policy["policy_text"] == TIGHTER_POLICY
    assert [c["id"] for c in policy["conditions"]] == [
        "c1_active_licence",
        "c2_board_certified",
        "c3_admitting",
    ]


def test_only_the_owner_can_change_the_policy(gate, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        gate.update_policy(
            GATE_ID, TIGHTER_POLICY, json.dumps(TIGHTER_CONDITIONS), json.dumps([])
        )


def test_policy_update_validates_the_new_conditions(gate, direct_vm, direct_owner):
    """A bad update must not be able to leave a gate in an unadjudicable state."""
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("cannot be empty"):
        gate.update_policy(GATE_ID, TIGHTER_POLICY, json.dumps([]), json.dumps([]))
    assert json.loads(gate.get_policy(GATE_ID))["policy_version"] == 1


def test_policy_update_rejects_a_too_short_text(gate, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("policy_text must be"):
        gate.update_policy(GATE_ID, "short", json.dumps(CONDITIONS), json.dumps([]))


def test_versions_increment_monotonically(gate, direct_vm, direct_owner):
    for expected in (2, 3, 4):
        assert int(bump_policy(gate, direct_vm, direct_owner)) == expected


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_policy_update_invalidates_a_live_grant_immediately(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_invalidated_record_still_exists_and_explains_itself(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The record is not deleted. It keeps its old version stamp, so the reason for the
    refusal is legible: the holder qualified under a policy that no longer applies.
    """
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "ACTIVE"
    assert record["policy_version"] == 1

    status = json.loads(gate.access_status(GATE_ID, direct_alice))
    assert status["approved"] is False
    assert status["reason"] == "POLICY_SUPERSEDED"
    assert status["record_policy_version"] == 1
    assert status["current_policy_version"] == 2


def test_one_update_invalidates_every_holder_at_once(
    gate, direct_vm, direct_owner, direct_accounts
):
    """
    The O(1) property. Six holders are invalidated by a single integer change, with no
    per holder write, which is what makes this usable for a gate serving an ecosystem.
    """
    holders = direct_accounts[:6]
    for account in holders:
        direct_vm.deal(account, 10**19)
        grant_to(gate, direct_vm, account)

    stats = json.loads(gate.gate_stats(GATE_ID))
    assert stats["live_holders"] == 6

    bump_policy(gate, direct_vm, direct_owner)

    for account in holders:
        assert gate.is_approved(GATE_ID, account) is False
    after = json.loads(gate.gate_stats(GATE_ID))
    assert after["live_holders"] == 0
    assert after["ever_granted_holders"] == 6


def test_holder_listing_reflects_invalidation(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    listed = json.loads(gate.list_holders(GATE_ID, 0, 10))["holders"][0]
    assert listed["live"] is True

    bump_policy(gate, direct_vm, direct_owner)
    listed = json.loads(gate.list_holders(GATE_ID, 0, 10))["holders"][0]
    assert listed["live"] is False
    assert listed["policy_version"] == 1


def test_mechanical_config_changes_do_not_invalidate(gate, direct_vm, direct_alice, direct_owner):
    """
    Changing the bond or the TTL is not a change to what the policy requires, so live
    grants survive it. Only update_policy invalidates.
    """
    grant_to(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_owner
    gate.update_gate_config(
        GATE_ID,
        json.dumps(["registry.example.com", "boards.example.org"]),
        "raw", True, True, 172800, BOND * 2, BOND * 4, 60,
    )
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_reapplying_under_the_new_policy_is_permitted(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is True
    assert pre["policy_version"] == 2

    app_id = apply_as(gate, direct_vm, direct_alice)
    assert json.loads(gate.get_application(app_id))["policy_version"] == 2


def test_readjudication_under_the_tighter_policy_can_deny(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The whole point of forcing re-adjudication: the same evidence that satisfied version
    one does not satisfy version two, so access is not restored automatically.
    """
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)

    app_id = apply_as(gate, direct_vm, direct_alice)
    token = gate.binding_token(GATE_ID, direct_alice)
    serve_page(direct_vm, licence_page(token))
    payload = json.loads(model_findings())
    payload["conditions"].append(
        {"id": "c3_admitting", "verdict": "NOT_SATISFIED", "quote": "", "note": "not stated"}
    )
    serve_model(direct_vm, json.dumps(payload))

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["failed_ids"] == ["c3_admitting"]
    assert gate.is_approved(GATE_ID, direct_alice) is False


def _findings_with_admitting():
    payload = json.loads(model_findings())
    payload["conditions"].append(
        {"id": "c3_admitting", "verdict": "NOT_SATISFIED", "quote": "", "note": "not stated"}
    )
    return json.dumps(payload)


def test_the_new_policy_text_reaches_the_validators(gate, direct_vm, direct_alice, direct_owner):
    """
    The prompt is built from stored policy bytes, so an update has to change what
    validators actually adjudicate, not merely what the gate advertises.

    The model mock is keyed on a phrase that exists only in the new policy. If the
    updated text did not reach the prompt the pattern would not match, no mock would be
    found, and the call would fail rather than quietly pass.
    """
    bump_policy(gate, direct_vm, direct_owner)
    app_id = apply_as(gate, direct_vm, direct_alice)

    serve_page(direct_vm, licence_page(gate.binding_token(GATE_ID, direct_alice)))
    direct_vm.mock_llm(
        r"(?s).*current hospital admitting privileges.*c3_admitting.*",
        _findings_with_admitting(),
    )
    result = json.loads(gate.adjudicate(app_id))
    assert result["failed_ids"] == ["c3_admitting"]


def test_the_superseded_policy_text_no_longer_reaches_the_validators(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The complement: the old wording is gone, not merely appended to. The only model
    mock is keyed on a phrase unique to version one, so the adjudication can only
    proceed if that stale text is still being sent, and it must not be.
    """
    bump_policy(gate, direct_vm, direct_owner)
    app_id = apply_as(gate, direct_vm, direct_alice)

    stale_phrase = "sanction recorded against that licence"
    assert stale_phrase in POLICY_TEXT
    assert stale_phrase not in TIGHTER_POLICY

    serve_page(direct_vm, licence_page(gate.binding_token(GATE_ID, direct_alice)))
    direct_vm.mock_llm(r"(?s).*" + stale_phrase + r".*", _findings_with_admitting())

    with pytest.raises(Exception) as exc:
        gate.adjudicate(app_id)
    assert "No LLM mock" in str(exc.value)


def test_invalidated_holder_can_reclaim_their_deposit(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    An honest holder whose access lapsed because the owner changed the rules is not
    punished. The deposit behind the record comes back in full.
    """
    grant_to(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)

    locked_before = int(json.loads(gate.get_registry_stats())["locked_wei"])
    direct_vm.sender = direct_alice
    refunded = gate.release_access(GATE_ID)
    assert int(refunded) == BOND

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == locked_before - BOND
    assert stats["treasury_wei"] == "0"


# ---------------------------------------------------------------------------
# Applications caught mid flight by an update
# ---------------------------------------------------------------------------


def test_application_filed_under_the_old_policy_is_refunded_not_judged(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    Fairness rule. An applicant who paid a bond against one set of conditions is not
    judged against conditions they never saw. The application closes and the bond is
    returned in full.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    assert json.loads(gate.get_application(app_id))["policy_version"] == 1

    bump_policy(gate, direct_vm, direct_owner)
    result = json.loads(gate.adjudicate(app_id))

    assert result["status"] == "SUPERSEDED"
    assert result["decision"] == ""
    assert int(result["refunded_wei"]) == BOND
    assert "version 1" in result["reason"] and "version 2" in result["reason"]


def test_superseded_application_takes_no_bond_and_sets_no_cooldown(
    gate, direct_vm, direct_alice, direct_owner
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    gate.adjudicate(app_id)

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == 0
    assert stats["treasury_wei"] == "0", "a superseded application is not a denial"

    gate_state = json.loads(gate.get_gate(GATE_ID))
    assert gate_state["total_denied"] == 0
    assert gate_state["total_granted"] == 0

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is True, "no cooldown should follow a supersede"


def test_superseded_application_needs_no_evidence_fetch(gate, direct_vm, direct_alice, direct_owner):
    """
    The supersede path short circuits before any network call. No web or LLM mock is
    installed here, so if it reached out the harness would fail the test.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    assert json.loads(gate.adjudicate(app_id))["status"] == "SUPERSEDED"


def test_applicant_can_immediately_refile_under_the_new_policy(
    gate, direct_vm, direct_alice, direct_owner
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    gate.adjudicate(app_id)

    refiled = apply_as(gate, direct_vm, direct_alice)
    assert refiled != app_id
    assert json.loads(gate.get_application(refiled))["policy_version"] == 2


def test_superseded_application_cannot_be_adjudicated_twice(
    gate, direct_vm, direct_alice, direct_owner
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    bump_policy(gate, direct_vm, direct_owner)
    gate.adjudicate(app_id)
    with direct_vm.expect_revert("already"):
        gate.adjudicate(app_id)
