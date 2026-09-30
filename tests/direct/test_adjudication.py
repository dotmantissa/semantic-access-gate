"""
Consensus adjudication.

This is the heart of the primitive. Each test runs the real contract's real
non-deterministic block inside the GenVM, with only the HTTP fetch and the model call
substituted. The validator half of the round is then driven with run_validator, which
replays the contract's own captured validator function.

The property being established is the one the whole design rests on: access is issued
only when a validator, fetching and reasoning independently, reaches the same decision
as the leader.
"""

import json

import pytest

from conftest import (
    BOND,
    EVIDENCE_URL,
    GATE_ID,
    SECOND_URL,
    TTL,
    apply_as,
    grant_everything,
    hex_of,
    licence_page,
    model_findings,
    register_default_gate,
    serve_model,
    serve_page,
)


def token_for(gate, applicant):
    return gate.binding_token(GATE_ID, applicant)


def setup_grant(gate, direct_vm, applicant):
    """Apply, then stage retrievable evidence and satisfied findings."""
    app_id = apply_as(gate, direct_vm, applicant)
    grant_everything(direct_vm, token_for(gate, applicant))
    return app_id


# ---------------------------------------------------------------------------
# The grant path
# ---------------------------------------------------------------------------


def test_qualified_applicant_is_granted_access(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    result = json.loads(gate.adjudicate(app_id))

    assert result["decision"] == "GRANTED"
    assert result["denial_code"] == ""
    assert result["binding_ok"] is True
    assert result["evidence_ok"] is True
    assert result["failed_ids"] == []
    assert gate.is_approved(GATE_ID, direct_alice) is True


def test_grant_creates_a_version_stamped_time_limited_record(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    record = json.loads(gate.get_access_record(GATE_ID, direct_alice))
    assert record["holder"].lower() == hex_of(direct_alice)
    assert record["status"] == "ACTIVE"
    assert record["policy_version"] == 1
    assert record["application_id"] == app_id
    assert int(record["expires_at"]) - int(record["granted_at"]) == TTL
    assert record["deposit_wei"] == str(BOND)


def test_bond_becomes_the_records_deposit_not_revenue(gate, direct_vm, direct_alice):
    """
    An honest applicant's bond is never taken. It converts into collateral behind the
    record, which is what a challenger can later win if the grant turns out to be wrong.
    """
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == BOND
    assert stats["treasury_wei"] == "0"
    assert json.loads(gate.get_gate(GATE_ID))["treasury_wei"] == "0"


def test_grant_writes_the_full_audit_trail_on_chain(gate, direct_vm, direct_alice):
    """
    The justification is stored, not just the outcome. A reviewer can see which
    condition was established by which quoted span.
    """
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    app = json.loads(gate.get_application(app_id))
    assert app["status"] == "GRANTED"
    assert app["decision"] == "GRANTED"
    ids = sorted(c["id"] for c in app["conditions"])
    assert ids == ["c1_active_licence", "c2_board_certified"]
    for condition in app["conditions"]:
        assert condition["verdict"] == "SATISFIED"
        assert condition["grounded"] is True
        assert condition["quote"]
    assert app["evidence_status"][0]["status"] == 200
    assert app["evidence_status"][0]["retrievable"] is True
    assert app["reasoning"]


def test_grant_updates_gate_counters_and_holder_index(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    stats = json.loads(gate.gate_stats(GATE_ID))
    assert stats["total_granted"] == 1
    assert stats["total_denied"] == 0
    assert stats["live_holders"] == 1
    assert stats["ever_granted_holders"] == 1

    holders = json.loads(gate.list_holders(GATE_ID, 0, 10))
    assert holders["total"] == 1
    assert holders["holders"][0]["holder"].lower() == hex_of(direct_alice)
    assert holders["holders"][0]["live"] is True


def test_access_status_reports_ok_for_a_live_grant(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    status = json.loads(gate.access_status(GATE_ID, direct_alice))
    assert status["approved"] is True
    assert status["reason"] == "OK"
    assert status["record_policy_version"] == status["current_policy_version"] == 1
    assert status["seconds_remaining"] == TTL
    assert status["has_open_challenge"] is False


def test_evidence_across_two_documents_is_combined(gate, direct_vm, direct_alice):
    """
    Conditions often live on different sites: a licence registry and a specialty board.
    The adjudication sees the union of the documents.
    """
    app_id = apply_as(gate, direct_vm, direct_alice, urls=[EVIDENCE_URL, SECOND_URL])
    token = token_for(gate, direct_alice)
    serve_page(direct_vm, "State Medical Board of Record.\nLicence status: ACTIVE and in good standing since 14 March 2011.\nWallet verification token: " + token, url=EVIDENCE_URL)
    serve_page(direct_vm, "Specialty board record.\nBoard certification: American Board of Internal Medicine, internal medicine.", url=SECOND_URL)
    serve_model(direct_vm, model_findings())

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
    assert len(result["evidence_status"]) == 2


# ---------------------------------------------------------------------------
# Denial paths
# ---------------------------------------------------------------------------


def test_unmet_condition_is_denied(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "CONDITIONS_NOT_SATISFIED"
    assert result["failed_ids"] == ["c1_active_licence"]
    assert gate.is_approved(GATE_ID, direct_alice) is False
    assert gate.get_access_record(GATE_ID, direct_alice) == ""


def test_denial_forfeits_the_bond_to_the_gate_treasury(gate, direct_vm, direct_alice):
    """Spamming a gate with unqualified applications has to cost something."""
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    gate.adjudicate(app_id)

    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == 0
    assert int(stats["treasury_wei"]) == BOND
    assert json.loads(gate.get_gate(GATE_ID))["treasury_wei"] == str(BOND)


def test_disqualifier_denies_an_otherwise_qualified_applicant(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), disciplined=True))
    serve_model(direct_vm, model_findings(disciplinary=True))

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "DISQUALIFIER_PRESENT"
    assert result["failed_ids"] == ["d1_disciplinary"]


def test_missing_binding_token_is_denied_without_a_model_call(gate, direct_vm, direct_alice):
    """
    Proof of wallet control is checked in code against the fetched bytes. No LLM mock
    is installed, so if the model were consulted the harness would fail the test.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page("glgate:med-licence:0xsomeoneelse"))

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "BINDING_TOKEN_MISSING"
    assert result["binding_ok"] is False
    assert result["conditions"] == []


def test_evidence_belonging_to_another_wallet_is_denied(gate, direct_vm, direct_alice, direct_bob):
    """
    The impersonation case. Bob publishes a page carrying Alice's token; Bob applying
    from his own address still fails, because the token names the address it authorises.
    """
    app_id = apply_as(gate, direct_vm, direct_bob)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice)))

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "BINDING_TOKEN_MISSING"


def test_unreachable_evidence_is_denied_not_errored(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, "gone", status=404)

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["denial_code"] == "EVIDENCE_UNRETRIEVABLE"
    assert result["evidence_ok"] is False


def test_ungrounded_claim_is_downgraded_and_denied(gate, direct_vm, direct_alice):
    """
    A model asserting a condition with a quote that is not in the document does not
    get a grant. The contract checks the quote against the bytes it fetched.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice)))
    serve_model(
        direct_vm,
        model_findings(c2_quote="holds an airline transport pilot certificate issued in 2015"),
    )

    result = json.loads(gate.adjudicate(app_id))
    assert result["decision"] == "DENIED"
    assert result["failed_ids"] == ["c2_board_certified"]
    downgraded = [c for c in result["conditions"] if c["id"] == "c2_board_certified"][0]
    assert downgraded["grounded"] is False
    assert "Downgraded" in downgraded["note"]


def test_denial_starts_the_reapply_cooldown(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    gate.adjudicate(app_id)

    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is False
    assert pre["reason"] == "COOLDOWN_ACTIVE"
    assert pre["cooldown_until"] > 0

    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("cooldown active"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "again")
    finally:
        direct_vm.value = 0


def test_transient_source_failure_reverts_and_changes_nothing(gate, direct_vm, direct_alice):
    """
    A rate limited or broken evidence host must not burn the applicant's bond. The
    transaction reverts, the application stays pending, and it can be retried.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, "", status=503)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("TRANSIENT"):
        gate.adjudicate(app_id)

    assert json.loads(gate.get_application(app_id))["status"] == "PENDING"
    assert int(json.loads(gate.get_registry_stats())["locked_wei"]) == BOND


def test_unparseable_model_output_reverts(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice)))
    serve_model(direct_vm, "I decline to produce JSON")

    with direct_vm.expect_revert("LLM_ERROR"):
        gate.adjudicate(app_id)
    assert json.loads(gate.get_application(app_id))["status"] == "PENDING"


# ---------------------------------------------------------------------------
# Adjudication access control and idempotence
# ---------------------------------------------------------------------------


def test_anyone_may_trigger_adjudication(gate, direct_vm, direct_alice, direct_charlie):
    """
    Adjudication is a public good. If only the owner could call it, a gate owner could
    stall an applicant indefinitely, which is exactly the discretion this removes.
    """
    app_id = setup_grant(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_charlie
    assert json.loads(gate.adjudicate(app_id))["decision"] == "GRANTED"


def test_adjudicating_twice_is_rejected(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)
    with direct_vm.expect_revert("already"):
        gate.adjudicate(app_id)


def test_adjudicating_an_unknown_application_is_rejected(gate, direct_vm):
    with direct_vm.expect_revert("Unknown application"):
        gate.adjudicate("app_404")


def test_a_granted_address_cannot_reapply_while_live(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["reason"] == "ALREADY_APPROVED"
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("already holds live access"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "again")
    finally:
        direct_vm.value = 0


# ---------------------------------------------------------------------------
# The validator rules, inside the VM
# ---------------------------------------------------------------------------


def test_validator_accepts_a_leader_it_can_reproduce(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)
    assert direct_vm.run_validator() is True


def test_validator_accepts_a_reproducible_denial(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    gate.adjudicate(app_id)
    assert direct_vm.run_validator() is True


def test_validator_rejects_a_forged_grant(gate, direct_vm, direct_alice):
    """
    A leader that claims a grant the evidence does not support is refused. Consensus
    rotates the leader instead of writing access to chain.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice), active=False))
    serve_model(direct_vm, model_findings(c1="NOT_SATISFIED"))
    gate.adjudicate(app_id)  # honest leader denies

    forged = {
        "decision": "GRANTED",
        "denial_code": "",
        "failed_ids": [],
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [
            {"id": "c1_active_licence", "verdict": "SATISFIED", "quote": "x", "grounded": True, "note": ""},
            {"id": "c2_board_certified", "verdict": "SATISFIED", "quote": "y", "grounded": True, "note": ""},
        ],
        "disqualifiers": [{"id": "d1_disciplinary", "present": False, "note": ""}],
        "evidence_status": [],
        "reasoning": "trust me",
    }
    assert direct_vm.run_validator(leader_result=forged) is False


def test_validator_rejects_a_forged_denial(gate, direct_vm, direct_alice):
    """Censorship is refused too: a leader cannot deny an applicant the evidence clears."""
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)  # honest leader grants

    forged = {
        "decision": "DENIED",
        "denial_code": "CONDITIONS_NOT_SATISFIED",
        "failed_ids": ["c1_active_licence"],
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [
            {"id": "c1_active_licence", "verdict": "NOT_SATISFIED", "quote": "", "grounded": False, "note": ""},
            {"id": "c2_board_certified", "verdict": "SATISFIED", "quote": "y", "grounded": True, "note": ""},
        ],
        "disqualifiers": [{"id": "d1_disciplinary", "present": False, "note": ""}],
        "evidence_status": [],
        "reasoning": "no",
    }
    assert direct_vm.run_validator(leader_result=forged) is False


def test_validator_rejects_an_audit_trail_that_contradicts_the_decision(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    inconsistent = {
        "decision": "GRANTED",
        "denial_code": "",
        "failed_ids": [],
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [
            {"id": "c1_active_licence", "verdict": "SATISFIED", "quote": "x", "grounded": True, "note": ""},
            {"id": "c2_board_certified", "verdict": "NOT_SATISFIED", "quote": "", "grounded": False, "note": ""},
        ],
        "disqualifiers": [{"id": "d1_disciplinary", "present": False, "note": ""}],
        "evidence_status": [],
        "reasoning": "",
    }
    assert direct_vm.run_validator(leader_result=inconsistent) is False


@pytest.mark.parametrize("bad", ["", "MAYBE", "approved", None])
def test_validator_rejects_an_out_of_range_decision(gate, direct_vm, direct_alice, bad):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)
    assert direct_vm.run_validator(leader_result={"decision": bad}) is False


def test_validator_rejects_a_non_dict_leader_result(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)
    assert direct_vm.run_validator(leader_result="GRANTED") is False


def test_validator_disagrees_when_it_reads_different_evidence(gate, direct_vm, direct_alice):
    """
    The case that makes independent verification more than a slogan. The leader saw a
    page establishing the policy; by the time the validator fetches, the page no longer
    carries the wallet token. The validator forms its own decision and disagrees, so
    no access is issued on evidence that cannot be reproduced.
    """
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    direct_vm.clear_mocks()
    serve_page(direct_vm, licence_page("glgate:med-licence:0xdifferent"))
    assert direct_vm.run_validator() is False


def test_validator_disagrees_when_evidence_disappears(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    direct_vm.clear_mocks()
    serve_page(direct_vm, "", status=404)
    assert direct_vm.run_validator() is False


# Agreement on a shared transient fault cannot be driven from here: the harness records
# the validator closure only after the leader function returns, so a leader that raises
# leaves nothing to replay. That path is covered against the real validator function in
# tests/unit/test_consensus_rules.py, and its on chain consequence, that a failed fetch
# changes no state and keeps the bond, is asserted above.


def test_validator_rejects_a_leader_error_it_cannot_reproduce(gate, direct_vm, direct_alice):
    app_id = setup_grant(gate, direct_vm, direct_alice)
    gate.adjudicate(app_id)

    from genlayer.gl.vm import UserError

    fabricated = UserError("[TRANSIENT] Evidence source returned 503 for nowhere")
    assert direct_vm.run_validator(leader_error=fabricated) is False


def test_adjudication_closures_are_serializable(gate, direct_vm, direct_alice):
    """
    The leader and validator functions cross a process boundary on a real network, so
    both must serialize. They deliberately capture a plain snapshot dict rather than the
    contract instance, because capturing self would pull a storage handle into the
    closure and fail to pickle.

    The harness's check_pickling flag is only consulted by run_nondet, not by
    run_nondet_unsafe, so the captured closures are pickled here directly rather than
    trusting the flag to have done it.
    """
    import cloudpickle

    app_id = setup_grant(gate, direct_vm, direct_alice)
    assert json.loads(gate.adjudicate(app_id))["decision"] == "GRANTED"

    captured = direct_vm._captured_validators
    assert captured, "adjudication did not register a consensus round"
    _result, leader_fn, validator_fn = captured[-1]
    for name, fn in (("leader_fn", leader_fn), ("validator_fn", validator_fn)):
        blob = cloudpickle.dumps(fn)
        assert blob, f"{name} produced no payload"
        assert cloudpickle.loads(blob) is not None

    # What this really guards against: a storage backed object inside the closure.
    for cell in leader_fn.__closure__ or ():
        assert not isinstance(cell.cell_contents, type(gate)), (
            "the contract instance leaked into the leader closure"
        )


def test_model_cannot_introduce_conditions_of_its_own(gate, direct_vm, direct_alice):
    """The contract iterates its stored policy, never the model's list of findings."""
    app_id = apply_as(gate, direct_vm, direct_alice)
    serve_page(direct_vm, licence_page(token_for(gate, direct_alice)))
    payload = json.loads(model_findings())
    payload["conditions"].append(
        {"id": "c99_invented", "verdict": "SATISFIED", "quote": "", "note": ""}
    )
    serve_model(direct_vm, json.dumps(payload))

    result = json.loads(gate.adjudicate(app_id))
    assert sorted(c["id"] for c in result["conditions"]) == [
        "c1_active_licence",
        "c2_board_certified",
    ]


def test_gate_without_binding_requirement_grants_on_evidence_alone(
    registry, direct_vm, direct_owner, direct_alice, funded
):
    """
    Some policies are about a document, not a wallet: a published sanctions list has no
    place to put a token. Binding is therefore per gate rather than mandatory.
    """
    register_default_gate(
        registry, direct_vm, direct_owner, gate_id="nobind-gate", binding_required=False
    )
    app_id = apply_as(registry, direct_vm, direct_alice, gate_id="nobind-gate")
    serve_page(direct_vm, licence_page("no token here at all"))
    serve_model(direct_vm, model_findings())

    result = json.loads(registry.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
    assert result["binding_ok"] is True
    assert registry.is_approved("nobind-gate", direct_alice) is True


def test_render_mode_gate_adjudicates_from_rendered_text(
    registry, direct_vm, direct_owner, direct_alice, funded
):
    register_default_gate(
        registry, direct_vm, direct_owner, gate_id="render-gate", fetch_mode="render"
    )
    app_id = apply_as(registry, direct_vm, direct_alice, gate_id="render-gate")
    token = registry.binding_token("render-gate", direct_alice)
    serve_page(direct_vm, licence_page(token))
    serve_model(direct_vm, model_findings())

    result = json.loads(registry.adjudicate(app_id))
    assert result["decision"] == "GRANTED"
