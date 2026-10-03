"""
Versioning and snapshotting of the adjudication frame.

A gate's eligibility rules are not only its prose. Four mechanical settings decide
who qualifies just as directly as the conditions do:

  allowed_hosts            which documents count as evidence at all
  fetch_mode               how that evidence is read
  binding_required         whether the applicant must prove control of the wallet
  require_grounded_quotes  whether a claim of compliance must be quotable

Treating those as mere configuration is what made two things possible that must not
be. A pending application could be adjudicated under a frame published after its
bond was posted - and because relaxing binding and grounding removes the only two
deterministic checks standing between evidence and a grant, that gave the gate owner
a way to manufacture a grant the evidence never earned, in a contract whose central
claim is that the owner can never grant access. And an existing grant survived a
change to the very requirements that established it.

The fix is both halves of what the defect needs. Those four settings are versioned:
changing any of them bumps rules_version, which invalidates every outstanding grant
on the next read exactly as a policy rewrite does. And each version pair publishes
an immutable RulesSnapshot that the adjudication reads instead of the live gate, so a
pending application is judged under the frame it agreed to or not judged at all.
"""

import json

import pytest

from conftest import (
    ALLOWED_HOSTS,
    BOND,
    CHALLENGE_STAKE,
    CONDITIONS,
    DISQUALIFIERS,
    EVIDENCE_URL,
    GATE_ID,
    POLICY_TEXT,
    SECOND_URL,
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


def set_config(
    contract,
    direct_vm,
    owner,
    gate_id=GATE_ID,
    hosts=None,
    fetch_mode="raw",
    binding_required=True,
    grounded=True,
    ttl=TTL,
    bond=BOND,
    stake=CHALLENGE_STAKE,
    cooldown=3600,
):
    """Call update_gate_config, defaulting every field to the registered gate's value."""
    direct_vm.sender = owner
    return int(
        contract.update_gate_config(
            gate_id,
            json.dumps(ALLOWED_HOSTS if hosts is None else hosts),
            fetch_mode,
            binding_required,
            grounded,
            ttl,
            bond,
            stake,
            cooldown,
        )
    )


def grant_to(contract, direct_vm, applicant, gate_id=GATE_ID):
    app_id = apply_as(contract, direct_vm, applicant, gate_id=gate_id)
    direct_vm.clear_mocks()
    grant_everything(direct_vm, contract.binding_token(gate_id, applicant))
    result = json.loads(contract.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
    return result


def rules_version(contract, gate_id=GATE_ID):
    return json.loads(contract.get_gate(gate_id))["rules_version"]


def policy_version(contract, gate_id=GATE_ID):
    return json.loads(contract.get_gate(gate_id))["policy_version"]


# ---------------------------------------------------------------------------
# Which changes move the version, and which do not
# ---------------------------------------------------------------------------


def test_a_new_gate_starts_at_rules_version_one(gate):
    assert rules_version(gate) == 1
    assert policy_version(gate) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("hosts", ["registry.example.com"]),
        ("fetch_mode", "render"),
        ("binding_required", False),
        ("grounded", False),
    ],
)
def test_each_adjudication_relevant_field_bumps_the_rules_version(
    gate, direct_vm, direct_owner, field, value
):
    """
    Every one of the four settings decides eligibility, so every one of them is a
    rules change on its own. None may be edited quietly.
    """
    assert set_config(gate, direct_vm, direct_owner, **{field: value}) == 2
    assert rules_version(gate) == 2


def test_an_economic_change_does_not_bump_the_rules_version(gate, direct_vm, direct_owner):
    """
    The price of applying and the lifetime of a grant are not eligibility rules.
    Editing them must not invalidate anyone, otherwise a gate owner could not reprice
    a gate without revoking every holder.
    """
    assert set_config(
        gate, direct_vm, direct_owner, ttl=172800, bond=BOND * 2, stake=BOND * 4, cooldown=60
    ) == 1
    assert rules_version(gate) == 1


def test_resubmitting_an_unchanged_frame_is_a_no_op(gate, direct_vm, direct_owner):
    for _ in range(3):
        assert set_config(gate, direct_vm, direct_owner) == 1


def test_reordering_the_host_list_is_not_a_change(gate, direct_vm, direct_owner):
    """
    Hosts are stored canonically sorted, so the comparison is against the form that
    would actually be written. Shuffling the input is not an eligibility change.
    """
    assert set_config(
        gate, direct_vm, direct_owner, hosts=list(reversed(ALLOWED_HOSTS))
    ) == 1


def test_a_duplicate_host_entry_is_not_a_change(gate, direct_vm, direct_owner):
    assert set_config(
        gate, direct_vm, direct_owner, hosts=ALLOWED_HOSTS + [ALLOWED_HOSTS[0]]
    ) == 1


def test_recasing_the_fetch_mode_is_not_a_change(gate, direct_vm, direct_owner):
    assert set_config(gate, direct_vm, direct_owner, fetch_mode="RAW") == 1


def test_rules_versions_increment_monotonically(gate, direct_vm, direct_owner):
    for expected, mode in ((2, "render"), (3, "raw"), (4, "render")):
        assert set_config(gate, direct_vm, direct_owner, fetch_mode=mode) == expected


def test_the_two_counters_are_independent(gate, direct_vm, direct_owner):
    """
    A policy rewrite and a frame change are different events and are counted
    separately, so an auditor can tell which one invalidated a grant.
    """
    direct_vm.sender = direct_owner
    gate.update_policy(
        GATE_ID, POLICY_TEXT + " Revised wording.", json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS)
    )
    assert (policy_version(gate), rules_version(gate)) == (2, 1)

    set_config(gate, direct_vm, direct_owner, fetch_mode="render")
    assert (policy_version(gate), rules_version(gate)) == (2, 2)


def test_only_the_owner_can_change_the_frame(gate, direct_vm, direct_bob):
    with direct_vm.expect_revert("Only the gate owner"):
        set_config(gate, direct_vm, direct_bob, binding_required=False)


def test_a_rejected_config_change_moves_nothing(gate, direct_vm, direct_owner):
    with direct_vm.expect_revert("fetch_mode must be"):
        set_config(gate, direct_vm, direct_owner, fetch_mode="screenshot")
    assert rules_version(gate) == 1


# ---------------------------------------------------------------------------
# Existing grants are invalidated when the eligibility rules change
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("hosts", ["registry.example.com"]),
        ("fetch_mode", "render"),
        ("binding_required", False),
        ("grounded", False),
    ],
)
def test_each_rules_change_invalidates_a_live_grant_immediately(
    gate, direct_vm, direct_alice, direct_owner, field, value
):
    grant_to(gate, direct_vm, direct_alice)
    assert gate.is_approved(GATE_ID, direct_alice) is True

    set_config(gate, direct_vm, direct_owner, **{field: value})
    assert gate.is_approved(GATE_ID, direct_alice) is False


def test_rules_supersession_has_its_own_reason_code(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    A consumer telling a user why they were turned away must be able to distinguish
    "the policy was rewritten" from "the way the policy is checked changed".
    """
    grant_to(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False)

    status = json.loads(gate.access_status(GATE_ID, direct_alice))
    assert status["approved"] is False
    assert status["reason"] == "RULES_SUPERSEDED"
    assert status["record_rules_version"] == 1
    assert status["current_rules_version"] == 2
    assert status["record_policy_version"] == status["current_policy_version"]


def test_the_invalidated_record_is_not_deleted(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, fetch_mode="render")

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "ACTIVE"
    assert record["rules_version"] == 1
    assert record["closed_at"] == 0


def test_one_rules_change_invalidates_every_holder_at_once(
    gate, direct_vm, direct_owner, direct_accounts
):
    """The O(1) property, for the frame as well as the policy."""
    holders = direct_accounts[:5]
    for account in holders:
        direct_vm.deal(account, 10**19)
        grant_to(gate, direct_vm, account)
    assert json.loads(gate.gate_stats(GATE_ID))["live_holders"] == 5

    set_config(gate, direct_vm, direct_owner, binding_required=False)

    for account in holders:
        assert gate.is_approved(GATE_ID, account) is False
    after = json.loads(gate.gate_stats(GATE_ID))
    assert after["live_holders"] == 0
    assert after["ever_granted_holders"] == 5


def test_holder_listing_reflects_a_rules_change(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    assert json.loads(gate.list_holders(GATE_ID, 0, 10))["holders"][0]["live"] is True

    set_config(gate, direct_vm, direct_owner, grounded=False)
    listing = json.loads(gate.list_holders(GATE_ID, 0, 10))
    assert listing["holders"][0]["live"] is False
    assert listing["holders"][0]["rules_version"] == 1
    assert listing["current_rules_version"] == 2


def test_an_invalidated_holder_can_reclaim_their_deposit(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    A holder whose access lapsed because the owner changed the rules is not punished
    for it. The deposit comes back in full.
    """
    grant_to(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False)

    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND
    assert audit(gate) == (0, 0)


def test_an_economic_change_leaves_grants_live(gate, direct_vm, direct_alice, direct_owner):
    grant_to(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, ttl=172800, bond=BOND * 3, cooldown=0)
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_an_invalidated_holder_may_requalify_under_the_new_frame(
    gate, direct_vm, direct_alice, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, fetch_mode="render")

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is True
    assert pre["rules_version"] == 2

    app_id = apply_as(gate, direct_vm, direct_alice)
    assert json.loads(gate.get_application(app_id))["rules_version"] == 2


# ---------------------------------------------------------------------------
# Pending applications cannot be judged under a frame that moved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("hosts", ["registry.example.com"]),
        ("fetch_mode", "render"),
        ("binding_required", False),
        ("grounded", False),
    ],
)
def test_a_rules_change_supersedes_a_pending_application(
    gate, direct_vm, direct_alice, direct_owner, field, value
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    assert json.loads(gate.get_application(app_id))["rules_version"] == 1

    set_config(gate, direct_vm, direct_owner, **{field: value})
    result = json.loads(gate.adjudicate(app_id))

    assert result["status"] == "SUPERSEDED"
    assert result["decision"] == ""
    assert int(result["refunded_wei"]) == BOND
    assert audit(gate) == (0, 0)


def test_the_supersede_names_the_frame_that_moved(gate, direct_vm, direct_alice, direct_owner):
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False)

    result = json.loads(gate.adjudicate(app_id))
    assert result["application_rules_version"] == 1
    assert result["current_rules_version"] == 2
    assert result["application_policy_version"] == result["current_policy_version"]
    assert "rules changed from version 1 to version 2" in result["reason"]


def test_a_relaxed_frame_cannot_manufacture_a_grant(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The security case, and the reason this is not merely a fairness bug.

    binding_required and require_grounded_quotes are the two deterministic checks
    that stand between a document and a grant: one proves the applicant controls the
    wallet, the other proves the model's claim of compliance is traceable to the
    fetched bytes. Switching both off after a bond is posted would let evidence that
    carries someone else's binding token, and quotes that appear nowhere in the page,
    be adjudicated into a live grant.

    That would make the gate owner able to grant access, which is the one thing this
    contract exists to make impossible. The bond was posted against a frame that
    required both checks, so that frame is the only one it can be judged under.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False, grounded=False)

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page("glgate:med-licence:0xsomebody-else-entirely"))
    serve_model(
        direct_vm,
        model_findings(
            c1_quote="a quote that appears nowhere in the page",
            c2_quote="also absent from the page",
        ),
    )

    result = json.loads(gate.adjudicate(app_id))
    assert result["status"] == "SUPERSEDED", "the relaxed frame was applied retroactively"
    assert result["decision"] == ""
    assert gate.is_approved(GATE_ID, direct_alice) is False
    assert audit(gate) == (0, 0)


def test_a_tightened_frame_cannot_seize_a_posted_bond(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The mirror image. An owner must not be able to narrow the evidence allowlist after
    a bond is posted, deny the applicant for submitting a URL that was permitted when
    they submitted it, and take the bond as forfeit.
    """
    app_id = apply_as(gate, direct_vm, direct_alice, urls=[EVIDENCE_URL, SECOND_URL])
    set_config(gate, direct_vm, direct_owner, hosts=["registry.example.com"])

    result = json.loads(gate.adjudicate(app_id))
    assert result["status"] == "SUPERSEDED"
    assert int(result["refunded_wei"]) == BOND
    assert json.loads(gate.get_gate(GATE_ID))["treasury_wei"] == "0", "not a denial"

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is True, "a supersede sets no cooldown"


def test_a_superseded_application_needs_no_evidence_fetch(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    No web or LLM mock is installed, so the harness fails the test if adjudication
    reaches the network. The frame check has to come first.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, fetch_mode="render")
    assert json.loads(gate.adjudicate(app_id))["status"] == "SUPERSEDED"


def test_the_applicant_can_refile_under_the_new_frame(
    gate, direct_vm, direct_alice, direct_owner
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False)
    gate.adjudicate(app_id)

    refiled = apply_as(gate, direct_vm, direct_alice)
    assert refiled != app_id
    assert json.loads(gate.get_application(refiled))["rules_version"] == 2


def test_a_superseded_application_cannot_be_adjudicated_twice(
    gate, direct_vm, direct_alice, direct_owner
):
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, grounded=False)
    gate.adjudicate(app_id)
    with direct_vm.expect_revert("already"):
        gate.adjudicate(app_id)


def test_an_economic_change_does_not_supersede_a_pending_application(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    Repricing a gate must not cancel work in flight. The application still adjudicates
    normally, under the bond it actually posted.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, bond=BOND * 5, ttl=172800)

    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
    assert int(json.loads(gate.get_access_record(GATE_ID, direct_alice))["deposit_wei"]) == BOND


# ---------------------------------------------------------------------------
# The published frame is immutable and remains readable
# ---------------------------------------------------------------------------


def test_a_published_frame_is_never_rewritten(gate, direct_vm, direct_owner):
    """
    The snapshot a pending application points at must be unreachable by the owner.
    After flipping every adjudication-relevant setting, version (1, 1) still reads
    back exactly as it was published.
    """
    before = json.loads(gate.get_rules(GATE_ID, 1, 1))
    assert before["binding_required"] is True
    assert before["require_grounded_quotes"] is True
    assert before["fetch_mode"] == "raw"
    assert before["allowed_hosts"] == sorted(ALLOWED_HOSTS)

    set_config(
        gate, direct_vm, direct_owner, hosts=["registry.example.com"],
        fetch_mode="render", binding_required=False, grounded=False,
    )

    assert json.loads(gate.get_rules(GATE_ID, 1, 1)) == before


def test_every_version_pair_stays_readable(gate, direct_vm, direct_owner):
    set_config(gate, direct_vm, direct_owner, fetch_mode="render")      # -> (1, 2)
    direct_vm.sender = direct_owner
    gate.update_policy(                                                # -> (2, 2)
        GATE_ID, POLICY_TEXT + " Revised wording.",
        json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
    )
    set_config(gate, direct_vm, direct_owner, fetch_mode="raw", ttl=TTL)  # -> (2, 3)

    assert json.loads(gate.get_rules(GATE_ID, 1, 1))["fetch_mode"] == "raw"
    assert json.loads(gate.get_rules(GATE_ID, 1, 2))["fetch_mode"] == "render"
    assert json.loads(gate.get_rules(GATE_ID, 2, 2))["fetch_mode"] == "render"
    assert json.loads(gate.get_rules(GATE_ID, 2, 3))["fetch_mode"] == "raw"
    assert json.loads(gate.get_rules(GATE_ID, 1, 1))["policy_text"] == POLICY_TEXT
    assert "Revised wording" in json.loads(gate.get_rules(GATE_ID, 2, 2))["policy_text"]


def test_an_unpublished_version_pair_reads_empty(gate):
    assert gate.get_rules(GATE_ID, 1, 1) != ""
    assert gate.get_rules(GATE_ID, 9, 9) == ""
    assert gate.get_rules("no-such-gate", 1, 1) == ""


def test_a_policy_update_publishes_a_frame_carrying_the_current_settings(
    gate, direct_vm, direct_owner
):
    """A policy bump republishes the whole frame, so the new pair is self-contained."""
    set_config(gate, direct_vm, direct_owner, binding_required=False)
    direct_vm.sender = direct_owner
    gate.update_policy(
        GATE_ID, POLICY_TEXT + " Revised wording.",
        json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
    )

    frame = json.loads(gate.get_rules(GATE_ID, 2, 2))
    assert frame["binding_required"] is False
    assert "Revised wording" in frame["policy_text"]


def test_get_policy_reports_the_whole_frame(gate):
    """
    Reading the prose alone is not reading the policy, so the view an applicant uses
    before spending a bond carries the mechanical requirements too.
    """
    policy = json.loads(gate.get_policy(GATE_ID))
    assert policy["policy_version"] == 1
    assert policy["rules_version"] == 1
    assert policy["binding_required"] is True
    assert policy["require_grounded_quotes"] is True
    assert policy["fetch_mode"] == "raw"
    assert policy["allowed_hosts"] == sorted(ALLOWED_HOSTS)


def test_the_frozen_frame_is_what_reaches_the_validators(
    gate, direct_vm, direct_alice, direct_owner
):
    """
    The snapshot is not decorative. A grant issued after an unrelated economic edit
    still has to satisfy the binding and grounding requirements of the frame the bond
    was posted under, and the only evidence served here does satisfy them.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    set_config(gate, direct_vm, direct_owner, bond=BOND * 2)  # no frame change

    direct_vm.clear_mocks()
    grant_everything(direct_vm, gate.binding_token(GATE_ID, direct_alice))
    result = json.loads(gate.adjudicate(app_id))

    assert result["decision"] == "GRANTED"
    assert result["binding_ok"] is True
    assert result["hosts_ok"] is True
    assert result["rules_version"] == 1
    assert json.loads(gate.get_access_record(GATE_ID, direct_alice))["rules_version"] == 1


# ---------------------------------------------------------------------------
# Open challenges are not settled under a frame that moved
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


def test_a_challenge_pins_the_record_and_frame_it_was_filed_against(
    gate, direct_vm, direct_alice, direct_bob
):
    app_id = grant_to(gate, direct_vm, direct_alice)["application_id"]
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)

    challenge = json.loads(gate.get_challenge(challenge_id))
    assert challenge["record_application_id"] == app_id
    assert challenge["policy_version"] == 1
    assert challenge["rules_version"] == 1


@pytest.mark.parametrize("mover", ["policy", "rules"])
def test_a_challenge_is_voided_when_the_frame_moves(
    gate, direct_vm, direct_alice, direct_bob, direct_owner, mover
):
    """
    A challenge is a staked claim about one record under one frame. If the gate moves
    on while it is open there is no shared set of rules left to decide it under, and
    settling it anyway would slash somebody under rules they never posted against.
    The stake is returned and nobody is punished.
    """
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)

    if mover == "policy":
        direct_vm.sender = direct_owner
        gate.update_policy(
            GATE_ID, POLICY_TEXT + " Revised wording.",
            json.dumps(CONDITIONS), json.dumps(DISQUALIFIERS),
        )
    else:
        set_config(gate, direct_vm, direct_owner, binding_required=False)

    result = json.loads(gate.resolve_challenge(challenge_id))
    assert result["status"] == "VOID"
    assert result["decision"] == ""
    assert int(result["challenger_payout_wei"]) == CHALLENGE_STAKE
    assert int(result["holder_payout_wei"]) == 0
    assert int(result["treasury_credit_wei"]) == 0


def test_a_voided_challenge_slashes_nobody(
    gate, direct_vm, direct_alice, direct_bob, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)
    set_config(gate, direct_vm, direct_owner, grounded=False)
    gate.resolve_challenge(challenge_id)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["status"] == "ACTIVE", "the record is left alone, already invalid by version"
    assert int(record["deposit_wei"]) == BOND
    assert json.loads(gate.get_gate(GATE_ID))["treasury_wei"] == "0"
    assert audit(gate) == (BOND, 0)

    # The holder's collateral is still hers, and the record is no longer approved.
    assert gate.is_approved(GATE_ID, direct_alice) is False
    direct_vm.sender = direct_alice
    assert int(gate.release_access(GATE_ID)) == BOND
    assert audit(gate) == (0, 0)


def test_a_voided_challenge_needs_no_evidence_fetch(
    gate, direct_vm, direct_alice, direct_bob, direct_owner
):
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)
    direct_vm.clear_mocks()
    set_config(gate, direct_vm, direct_owner, fetch_mode="render")
    assert json.loads(gate.resolve_challenge(challenge_id))["status"] == "VOID"


def test_a_voided_challenge_clears_the_open_slot(
    gate, direct_vm, direct_alice, direct_bob, direct_owner
):
    """After a void the holder is free again: nothing is left blocking them."""
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)
    set_config(gate, direct_vm, direct_owner, binding_required=False)
    gate.resolve_challenge(challenge_id)

    status = json.loads(gate.access_status(GATE_ID, direct_alice))
    assert status["has_open_challenge"] is False
    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["eligible"] is True

    with direct_vm.expect_revert("already"):
        gate.resolve_challenge(challenge_id)


def test_an_unaffected_challenge_still_resolves_normally(
    gate, direct_vm, direct_alice, direct_bob, direct_owner
):
    """
    The void path must not swallow legitimate challenges. An economic edit does not
    move the frame, so the challenge decides on the evidence exactly as before.
    """
    grant_to(gate, direct_vm, direct_alice)
    challenge_id = open_challenge(gate, direct_vm, direct_bob, direct_alice)
    set_config(gate, direct_vm, direct_owner, bond=BOND * 2, cooldown=0)

    direct_vm.clear_mocks()
    serve_page(
        direct_vm,
        licence_page(gate.binding_token(GATE_ID, direct_alice), disciplined=True),
        url=EVIDENCE_URL,
    )
    serve_page(direct_vm, "Disciplinary notice: suspension recorded 2019.", url=CHALLENGE_URL)
    serve_model(direct_vm, model_findings(disciplinary=True))

    result = json.loads(gate.resolve_challenge(challenge_id))
    assert result["status"] == "UPHELD"
    assert gate.is_approved(GATE_ID, direct_alice) is False
    audit(gate)
