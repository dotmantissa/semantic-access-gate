"""
Tests for the adjudication round and the three validator rules.

Direct mode executes the leader function only, so this is where the validator half
of consensus is verified: that an honest leader is accepted, that a leader whose
published audit trail contradicts its own decision is rejected, that a leader whose
decision the validator cannot independently reproduce is rejected, and that the deny
path tolerates differing reasons instead of deadlocking.

Web and LLM calls are the only things substituted. The adjudication procedure, the
grounding check, the binding check and the reducer are the shipped implementations.
"""

import json

import pytest


APPLICANT = "0xAAaa000000000000000000000000000000000001"
GATE = "med-licence"
TOKEN = "glgate:med-licence:0xaaaa000000000000000000000000000000000001"

LICENCE_PAGE = (
    "Jane Okafor, MD. Board certified in internal medicine, licence number IM-884213, "
    "active and in good standing with the State Medical Board since 2011. "
    "No disciplinary actions on record. "
    "Wallet verification: " + TOKEN
)

CONDITIONS = [
    {"id": "c1_licensed", "text": "Holds an active medical licence in good standing"},
    {"id": "c2_specialty", "text": "Is board certified in a recognised specialty"},
]
DISQUALIFIERS = [
    {"id": "d1_discipline", "text": "Has a disciplinary action or suspension on record"}
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_spec(**overrides):
    spec = {
        "policy_text": "Access is limited to actively licensed medical professionals "
        "who are board certified and free of disciplinary history.",
        "conditions": [dict(c) for c in CONDITIONS],
        "disqualifiers": [dict(d) for d in DISQUALIFIERS],
        "allowed_hosts": [],
        "fetch_mode": "raw",
        "binding_required": True,
        "require_grounded_quotes": True,
        "binding_token": TOKEN,
        "evidence_urls": ["https://registry.example.com/jane-okafor"],
        "applicant_note": "Internal medicine, licensed since 2011.",
    }
    spec.update(overrides)
    return spec


def serve(stub, pages):
    """Install a web handler that serves a url -> (status, body) map."""

    def handler(url):
        status, body = pages.get(url, (404, ""))
        return stub.WebResponse(status, body.encode() if isinstance(body, str) else body)

    stub._Web.get_handler = handler


def respond(stub, payload):
    """Install an LLM handler returning a fixed payload for any prompt."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    stub._Nondet.prompt_handler = lambda prompt: text


def model_grants():
    return {
        "conditions": [
            {
                "id": "c1_licensed",
                "verdict": "SATISFIED",
                "quote": "active and in good standing with the State Medical Board since 2011",
                "note": "registry lists active status",
            },
            {
                "id": "c2_specialty",
                "verdict": "SATISFIED",
                "quote": "Board certified in internal medicine, licence number IM-884213",
                "note": "board certification stated",
            },
        ],
        "disqualifiers": [{"id": "d1_discipline", "present": False, "note": "none listed"}],
        "reasoning": "Registry record establishes both conditions.",
    }


def model_denies(failing_id="c2_specialty"):
    payload = model_grants()
    for entry in payload["conditions"]:
        if entry["id"] == failing_id:
            entry["verdict"] = "NOT_SATISFIED"
            entry["quote"] = ""
    payload["reasoning"] = "One condition is not established by the evidence."
    return payload


def consistent_leader_output(module, decision, conditions, disqualifiers, **extra):
    """
    Build a leader payload whose audit trail genuinely reduces to `decision`, so
    rule 2 passes and rule 3 is the only thing left to decide agreement.
    """
    binding_ok = extra.get("binding_ok", True)
    evidence_ok = extra.get("evidence_ok", True)
    hosts_ok = extra.get("hosts_ok", True)
    reduced, code, failed = module._reduce_decision(
        conditions, disqualifiers, binding_ok, evidence_ok, hosts_ok
    )
    assert reduced == decision, "test helper built an inconsistent payload"
    return {
        "decision": decision,
        "denial_code": code,
        "failed_ids": failed,
        "binding_ok": binding_ok,
        "evidence_ok": evidence_ok,
        "hosts_ok": hosts_ok,
        "conditions": conditions,
        "disqualifiers": disqualifiers,
        "evidence_status": [
            {
                "url": "https://registry.example.com/jane-okafor",
                "status": 200,
                "retrievable": True,
                "admissible": True,
            }
        ],
        "reasoning": "leader",
    }


# ---------------------------------------------------------------------------
# Evidence retrieval and status classification
# ---------------------------------------------------------------------------


def test_successful_fetch_is_retrievable(contract_module, stub):
    serve(stub, {"https://a.example.com/x": (200, "hello world")})
    docs = contract_module._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert docs[0]["retrievable"] is True
    assert docs[0]["text"] == "hello world"
    assert docs[0]["status"] == 200


@pytest.mark.parametrize("status", [401, 403, 404, 410, 451])
def test_deterministic_absence_denies_rather_than_erroring(contract_module, stub, status):
    """
    A missing or forbidden document is the same for every node, so it resolves to a
    denial. Turning it into an error would revert the transaction and strand the bond.
    """
    serve(stub, {"https://a.example.com/x": (status, "nope")})
    docs = contract_module._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert docs[0]["retrievable"] is False
    assert docs[0]["text"] == ""


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_faults_raise_a_classified_error(contract_module, stub, status):
    """
    Rate limits and server faults are not evidence of anything. Both nodes raise, the
    transaction reverts with no state change, and the adjudication can be retried.
    """
    m = contract_module
    serve(stub, {"https://a.example.com/x": (status, "")})
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert exc.value.message.startswith(m.ERROR_TRANSIENT)


def test_empty_body_with_200_is_not_retrievable(contract_module, stub):
    serve(stub, {"https://a.example.com/x": (200, "   ")})
    docs = contract_module._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert docs[0]["retrievable"] is False


def test_network_exception_raises_transient(contract_module, stub):
    m = contract_module

    def boom(url):
        raise RuntimeError("connection reset")

    stub._Web.get_handler = boom
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert exc.value.message.startswith(m.ERROR_TRANSIENT)


def test_evidence_text_is_truncated_to_the_limit(contract_module, stub):
    m = contract_module
    serve(stub, {"https://a.example.com/x": (200, "y" * 10000)})
    docs = m._fetch_evidence(["https://a.example.com/x"], "raw", [])
    assert len(docs[0]["text"]) == m.EVIDENCE_TEXT_LIMIT


def test_render_mode_extracts_text(contract_module, stub):
    stub._Web.render_handler = lambda url, mode: "rendered content here"
    docs = contract_module._fetch_evidence(["https://a.example.com/x"], "render", [])
    assert docs[0]["retrievable"] is True
    assert docs[0]["text"] == "rendered content here"


def test_render_failure_raises_transient(contract_module, stub):
    m = contract_module

    def boom(url, mode):
        raise RuntimeError("headless crash")

    stub._Web.render_handler = boom
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._fetch_evidence(["https://a.example.com/x"], "render", [])
    assert exc.value.message.startswith(m.ERROR_TRANSIENT)


def test_multiple_documents_are_fetched_in_order(contract_module, stub):
    serve(
        stub,
        {
            "https://a.example.com/1": (200, "first"),
            "https://a.example.com/2": (200, "second"),
        },
    )
    docs = contract_module._fetch_evidence(
        ["https://a.example.com/1", "https://a.example.com/2"], "raw", []
    )
    assert [d["text"] for d in docs] == ["first", "second"]


# ---------------------------------------------------------------------------
# Adjudication branches
# ---------------------------------------------------------------------------


def test_adjudication_grants_on_satisfying_evidence(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_GRANTED
    assert out["denial_code"] == m.DENIAL_NONE
    assert out["binding_ok"] is True
    assert out["evidence_ok"] is True
    assert all(c["grounded"] for c in out["conditions"])


def test_adjudication_denies_when_a_condition_is_unmet(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_denies("c2_specialty"))

    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_CONDITIONS
    assert out["failed_ids"] == ["c2_specialty"]


def test_missing_binding_token_denies_without_consulting_the_model(contract_module, stub):
    """
    Proof of address control is checked in code over the fetched bytes. The model is
    never reached, so this denial cannot be argued around or hallucinated away. The
    LLM handler is deliberately left uninstalled: if it were called the test errors.
    """
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, "A page about someone with no token")})

    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_BINDING
    assert out["binding_ok"] is False
    assert out["conditions"] == []


def test_binding_token_match_is_case_insensitive(contract_module, stub):
    m = contract_module
    spec = make_spec()
    shouted = LICENCE_PAGE.replace(TOKEN, TOKEN.upper())
    serve(stub, {spec["evidence_urls"][0]: (200, shouted)})
    respond(stub, model_grants())
    out = m._run_adjudication(spec)
    assert out["binding_ok"] is True


def test_another_addresses_token_does_not_satisfy_binding(contract_module, stub):
    """Republished evidence belonging to a different wallet must not pass."""
    m = contract_module
    spec = make_spec()
    foreign = LICENCE_PAGE.replace(
        TOKEN, "glgate:med-licence:0xbbbb000000000000000000000000000000000002"
    )
    serve(stub, {spec["evidence_urls"][0]: (200, foreign)})
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_BINDING


def test_unretrievable_evidence_denies_without_consulting_the_model(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {})  # every url 404s
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_UNRETRIEVABLE
    assert out["evidence_ok"] is False


def test_binding_can_be_disabled_per_gate(contract_module, stub):
    m = contract_module
    spec = make_spec(binding_required=False)
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE.replace(TOKEN, ""))})
    respond(stub, model_grants())
    out = m._run_adjudication(spec)
    assert out["binding_ok"] is True
    assert out["decision"] == m.DECISION_GRANTED


def test_disqualifier_denies_an_otherwise_qualified_applicant(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["disqualifiers"] = [
        {"id": "d1_discipline", "present": True, "note": "suspension recorded 2019"}
    ]
    respond(stub, payload)

    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_DISQUALIFIED
    assert out["failed_ids"] == ["d1_discipline"]


def test_ungrounded_claim_is_downgraded_in_code(contract_module, stub):
    """
    The anti-hallucination rule. A model asserting compliance with a quote that is not
    in the fetched bytes does not get a grant; the contract downgrades the verdict.
    """
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"][1]["quote"] = (
        "holds a valid airline transport pilot certificate issued in 2015"
    )
    respond(stub, payload)

    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_CONDITIONS
    assert out["failed_ids"] == ["c2_specialty"]
    downgraded = [c for c in out["conditions"] if c["id"] == "c2_specialty"][0]
    assert downgraded["grounded"] is False
    assert "Downgraded" in downgraded["note"]


def test_satisfied_verdict_with_no_quote_is_downgraded(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"][0]["quote"] = ""
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["failed_ids"] == ["c1_licensed"]


def test_quote_shorter_than_the_grounding_floor_is_downgraded(contract_module, stub):
    """A two word quote is not evidence, even when it does appear in the document."""
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"][0]["quote"] = "Jane"
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["failed_ids"] == ["c1_licensed"]


def test_grounding_survives_reflowed_whitespace_in_the_quote(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"][0]["quote"] = (
        "active and in good standing\n  with the State Medical Board since 2011"
    )
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_GRANTED


def test_grounding_can_be_disabled_per_gate(contract_module, stub):
    m = contract_module
    spec = make_spec(require_grounded_quotes=False)
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"][1]["quote"] = "a quote that appears nowhere in the document"
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_GRANTED


def test_condition_omitted_by_the_model_counts_as_unmet(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"] = [payload["conditions"][0]]
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_DENIED
    assert out["failed_ids"] == ["c2_specialty"]


def test_unknown_condition_ids_from_the_model_are_ignored(contract_module, stub):
    """
    The contract iterates its own policy, not the model's answer, so a model cannot
    introduce conditions or vote on ones that do not exist.
    """
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    payload = model_grants()
    payload["conditions"].append(
        {"id": "c99_invented", "verdict": "SATISFIED", "quote": "x", "note": ""}
    )
    respond(stub, payload)
    out = m._run_adjudication(spec)
    assert out["decision"] == m.DECISION_GRANTED
    assert [c["id"] for c in out["conditions"]] == ["c1_licensed", "c2_specialty"]


def test_prompt_never_asks_the_model_for_the_outcome(contract_module, stub):
    """
    The decision is computed by the reducer, never returned by the model. The prompt
    must not offer the model a field that decides access.
    """
    m = contract_module
    spec = make_spec()
    docs = [{"url": "u", "status": 200, "retrievable": True, "text": LICENCE_PAGE}]
    prompt = m._build_prompt(spec, docs)
    assert '"decision"' not in prompt
    assert "GRANTED" not in prompt
    assert "verbatim" in prompt.lower()
    for condition in CONDITIONS:
        assert condition["id"] in prompt
        assert condition["text"] in prompt


def test_prompt_marks_unretrievable_documents_explicitly(contract_module):
    m = contract_module
    spec = make_spec()
    docs = [{"url": "https://x.example/y", "status": 404, "retrievable": False, "text": ""}]
    prompt = m._build_prompt(spec, docs)
    assert "COULD NOT BE RETRIEVED" in prompt


def test_unparseable_model_output_raises_llm_error(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, "I am not going to answer that")
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._run_adjudication(spec)
    assert exc.value.message.startswith(m.ERROR_LLM)


def test_adjudication_is_reproducible_across_repeated_runs(contract_module, stub):
    """The property validators rely on: same inputs, same decision, every time."""
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())
    first = m._run_adjudication(spec)
    for _ in range(10):
        assert m._run_adjudication(spec)["decision"] == first["decision"]


# ---------------------------------------------------------------------------
# Admissibility inside a full adjudication round
# ---------------------------------------------------------------------------


def test_an_inadmissible_host_denies_without_consulting_the_model(contract_module, stub):
    """
    The frame's allowlist is applied before anything is fetched or prompted, so this
    resolves to a denial with no network call and no model call at all. No LLM handler
    is installed, so reaching the model would raise instead of passing.
    """
    m = contract_module
    spec = make_spec(
        evidence_urls=["https://elsewhere.example.net/jane"],
        allowed_hosts=["registry.example.com"],
    )
    out = m._run_adjudication(spec)

    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_HOST_NOT_ALLOWED
    assert out["hosts_ok"] is False
    assert out["conditions"] == []
    assert out["evidence_status"][0]["admissible"] is False
    assert "allowlist" in out["reasoning"]


def test_one_inadmissible_url_fails_the_whole_evidence_set(contract_module, stub):
    """
    A partially admissible bundle is not quietly adjudicated on the admissible half.
    An applicant is told their submission was rejected rather than silently judged on
    less than they filed.
    """
    m = contract_module
    serve(stub, {"https://registry.example.com/jane-okafor": (200, LICENCE_PAGE)})
    respond(stub, model_grants())
    spec = make_spec(
        evidence_urls=[
            "https://registry.example.com/jane-okafor",
            "https://elsewhere.example.net/extra",
        ],
        allowed_hosts=["registry.example.com"],
    )
    out = m._run_adjudication(spec)

    assert out["decision"] == m.DECISION_DENIED
    assert out["denial_code"] == m.DENIAL_HOST_NOT_ALLOWED


def test_an_admissible_bundle_grants_normally(contract_module, stub):
    """The allowlist must gate nothing it permits."""
    m = contract_module
    serve(stub, {"https://registry.example.com/jane-okafor": (200, LICENCE_PAGE)})
    respond(stub, model_grants())
    out = m._run_adjudication(
        make_spec(allowed_hosts=["registry.example.com", "boards.example.org"])
    )
    assert out["decision"] == m.DECISION_GRANTED
    assert out["hosts_ok"] is True


def test_the_prompt_labels_an_inadmissible_document_as_such(contract_module):
    """
    An inadmissible document must not be presented to the model as a dead link, since
    those are different facts and only one of them is about the applicant's evidence.
    """
    m = contract_module
    prompt = m._build_prompt(
        make_spec(),
        [
            {
                "url": "https://elsewhere.example.net/x",
                "status": 0,
                "retrievable": False,
                "admissible": False,
                "text": "",
            }
        ],
    )
    assert "NOT ADMISSIBLE" in prompt
    assert "COULD NOT BE RETRIEVED" not in prompt


def test_rule_two_rejects_a_leader_hiding_an_inadmissible_bundle(contract_module):
    """
    Rule 2 re-reduces the leader's own reported findings, admissibility included. A
    leader cannot report hosts_ok=False and still have a GRANTED decision accepted.
    """
    m = contract_module
    payload = consistent_leader_output(
        m, m.DECISION_GRANTED, model_grants()["conditions"], model_grants()["disqualifiers"]
    )
    payload["hosts_ok"] = False
    assert m._leader_audit_is_consistent(payload) is False


def test_rule_two_accepts_a_consistent_inadmissibility_denial(contract_module):
    m = contract_module
    payload = consistent_leader_output(
        m, m.DECISION_DENIED, [], [], hosts_ok=False
    )
    assert payload["denial_code"] == m.DENIAL_HOST_NOT_ALLOWED
    assert m._leader_audit_is_consistent(payload) is True


def test_rule_two_rejects_a_mislabelled_inadmissibility_denial(contract_module):
    """
    The denial code is part of the audit trail, so a leader cannot deny for an
    inadmissible host while telling the chain the evidence was merely unreachable.
    """
    m = contract_module
    payload = consistent_leader_output(m, m.DECISION_DENIED, [], [], hosts_ok=False)
    payload["denial_code"] = m.DENIAL_UNRETRIEVABLE
    assert m._leader_audit_is_consistent(payload) is False


def test_a_validator_reproduces_an_inadmissibility_denial(contract_module, stub):
    """
    Rule 3 end to end on this branch: the frame is in the spec both nodes share, so a
    validator derives the same denial from it without seeing the leader's answer.
    """
    m = contract_module
    spec = make_spec(
        evidence_urls=["https://elsewhere.example.net/jane"],
        allowed_hosts=["registry.example.com"],
    )
    leader_output = m._run_adjudication(spec)
    own = m._run_adjudication(spec)
    assert leader_output["decision"] == own["decision"]
    assert m._leader_audit_is_consistent(leader_output) is True


# ---------------------------------------------------------------------------
# Rule 2: audit integrity
# ---------------------------------------------------------------------------


def test_consistent_grant_audit_passes(contract_module):
    m = contract_module
    payload = consistent_leader_output(
        m,
        m.DECISION_GRANTED,
        [
            {"id": "c1_licensed", "verdict": "SATISFIED"},
            {"id": "c2_specialty", "verdict": "SATISFIED"},
        ],
        [{"id": "d1_discipline", "present": False}],
    )
    assert m._leader_audit_is_consistent(payload) is True


def test_grant_contradicted_by_its_own_findings_is_rejected(contract_module):
    """
    A leader cannot publish a grant while its audit trail records a failed condition.
    Without this rule the permanent on-chain justification could disagree with the
    access it justifies.
    """
    m = contract_module
    payload = {
        "decision": m.DECISION_GRANTED,
        "denial_code": m.DENIAL_NONE,
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [
            {"id": "c1_licensed", "verdict": "SATISFIED"},
            {"id": "c2_specialty", "verdict": "NOT_SATISFIED"},
        ],
        "disqualifiers": [],
    }
    assert m._leader_audit_is_consistent(payload) is False


def test_grant_claimed_over_a_present_disqualifier_is_rejected(contract_module):
    m = contract_module
    payload = {
        "decision": m.DECISION_GRANTED,
        "denial_code": m.DENIAL_NONE,
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [{"id": "c1_licensed", "verdict": "SATISFIED"}],
        "disqualifiers": [{"id": "d1_discipline", "present": True}],
    }
    assert m._leader_audit_is_consistent(payload) is False


def test_grant_claimed_despite_failed_binding_is_rejected(contract_module):
    m = contract_module
    payload = {
        "decision": m.DECISION_GRANTED,
        "denial_code": m.DENIAL_NONE,
        "binding_ok": False,
        "evidence_ok": True,
        "conditions": [{"id": "c1_licensed", "verdict": "SATISFIED"}],
        "disqualifiers": [],
    }
    assert m._leader_audit_is_consistent(payload) is False


def test_denial_with_the_wrong_reason_code_is_rejected(contract_module):
    """The reason recorded on chain has to be the reason the findings support."""
    m = contract_module
    payload = {
        "decision": m.DECISION_DENIED,
        "denial_code": m.DENIAL_BINDING,
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [{"id": "c1_licensed", "verdict": "NOT_SATISFIED"}],
        "disqualifiers": [],
    }
    assert m._leader_audit_is_consistent(payload) is False


def test_clean_audit_paired_with_a_denial_is_rejected(contract_module):
    m = contract_module
    payload = {
        "decision": m.DECISION_DENIED,
        "denial_code": m.DENIAL_CONDITIONS,
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [{"id": "c1_licensed", "verdict": "SATISFIED"}],
        "disqualifiers": [],
    }
    assert m._leader_audit_is_consistent(payload) is False


def test_non_list_findings_are_rejected(contract_module):
    m = contract_module
    assert (
        m._leader_audit_is_consistent(
            {"decision": "GRANTED", "conditions": "nope", "disqualifiers": []}
        )
        is False
    )


def test_malformed_findings_do_not_raise_inside_the_validator(contract_module):
    """
    Rule 2 runs over leader-supplied data. It has to stay total: a leader that sends
    garbage must be rejected, never able to throw inside every validator.
    """
    m = contract_module
    payload = {
        "decision": m.DECISION_GRANTED,
        "denial_code": m.DENIAL_NONE,
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": ["not-a-dict", 42, None],
        "disqualifiers": [None],
    }
    assert m._leader_audit_is_consistent(payload) is False


# ---------------------------------------------------------------------------
# The full validator function
# ---------------------------------------------------------------------------


def capture_validator(module, stub, spec):
    """Run one consensus round and hand back the leader result and validator fn."""
    leader_result = module._adjudicate_with_consensus(spec)
    return leader_result, stub._VM.last_validator


def test_validator_agrees_with_an_honest_grant(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    leader_result, validator = capture_validator(m, stub, spec)
    assert leader_result["decision"] == m.DECISION_GRANTED
    assert validator(stub.Return(leader_result)) is True


def test_validator_agrees_with_an_honest_denial(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_denies())

    leader_result, validator = capture_validator(m, stub, spec)
    assert leader_result["decision"] == m.DECISION_DENIED
    assert validator(stub.Return(leader_result)) is True


def test_validator_rejects_a_grant_it_cannot_reproduce(contract_module, stub):
    """
    Rule 3, the core of the primitive. The validator adjudicates for itself. A leader
    claiming a grant the validator's own reading does not support is rejected, and
    consensus rotates the leader rather than issuing access.
    """
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_denies())  # every node's own reading denies

    _leader_result, validator = capture_validator(m, stub, spec)
    forged = consistent_leader_output(
        m,
        m.DECISION_GRANTED,
        [
            {"id": "c1_licensed", "verdict": "SATISFIED"},
            {"id": "c2_specialty", "verdict": "SATISFIED"},
        ],
        [{"id": "d1_discipline", "present": False}],
    )
    assert validator(stub.Return(forged)) is False


def test_validator_rejects_a_denial_it_cannot_reproduce(contract_module, stub):
    """Censorship is refused as firmly as forgery: a leader cannot deny a qualified
    applicant when validators independently find the policy satisfied."""
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())  # every node's own reading grants

    _leader_result, validator = capture_validator(m, stub, spec)
    forged = consistent_leader_output(
        m,
        m.DECISION_DENIED,
        [
            {"id": "c1_licensed", "verdict": "SATISFIED"},
            {"id": "c2_specialty", "verdict": "NOT_SATISFIED"},
        ],
        [{"id": "d1_discipline", "present": False}],
    )
    assert validator(stub.Return(forged)) is False


def test_validator_rejects_an_inconsistent_audit_trail(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    _leader_result, validator = capture_validator(m, stub, spec)
    tampered = {
        "decision": m.DECISION_GRANTED,
        "denial_code": m.DENIAL_NONE,
        "failed_ids": [],
        "binding_ok": True,
        "evidence_ok": True,
        "conditions": [
            {"id": "c1_licensed", "verdict": "SATISFIED"},
            {"id": "c2_specialty", "verdict": "NOT_SATISFIED"},
        ],
        "disqualifiers": [],
        "evidence_status": [],
        "reasoning": "",
    }
    assert validator(stub.Return(tampered)) is False


@pytest.mark.parametrize("bad", ["", "MAYBE", "granted", "APPROVED", None, 1])
def test_validator_rejects_an_out_of_range_decision(contract_module, stub, bad):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    leader_result, validator = capture_validator(m, stub, spec)
    payload = dict(leader_result)
    payload["decision"] = bad
    assert validator(stub.Return(payload)) is False


def test_validator_rejects_a_non_dict_payload(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    _leader_result, validator = capture_validator(m, stub, spec)
    assert validator(stub.Return("GRANTED")) is False
    assert validator(stub.Return(None)) is False
    assert validator(stub.Return([1, 2])) is False


def test_denials_agree_even_when_the_reasons_differ(contract_module, stub):
    """
    The deny path must not deadlock. Two honest nodes can fail an applicant on
    different conditions and still settle, because only the decision is compared.
    """
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_denies("c2_specialty"))  # validator fails c2

    _leader_result, validator = capture_validator(m, stub, spec)
    other_reason = consistent_leader_output(
        m,
        m.DECISION_DENIED,
        [
            {"id": "c1_licensed", "verdict": "NOT_SATISFIED"},
            {"id": "c2_specialty", "verdict": "SATISFIED"},
        ],
        [{"id": "d1_discipline", "present": False}],
    )
    assert validator(stub.Return(other_reason)) is True


# ---------------------------------------------------------------------------
# Error agreement
# ---------------------------------------------------------------------------


def test_shared_transient_fault_is_agreement(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (503, "")})

    with pytest.raises(m.gl.vm.UserError):
        m._adjudicate_with_consensus(spec)
    validator = stub._VM.last_validator

    leader_error = stub.Rollback(f"{m.ERROR_TRANSIENT} Evidence source returned 502")
    assert validator(leader_error) is True


def test_identical_expected_errors_are_agreement(contract_module, stub):
    m = contract_module
    spec = make_spec()

    def boom(url):
        raise RuntimeError("x")

    stub._Web.get_handler = boom
    with pytest.raises(m.gl.vm.UserError):
        m._adjudicate_with_consensus(spec)
    validator = stub._VM.last_validator

    same = stub.Rollback(
        f"{m.ERROR_TRANSIENT} Evidence fetch failed for {spec['evidence_urls'][0]}: x"
    )
    assert validator(same) is True


def test_leader_error_the_validator_cannot_reproduce_is_disagreement(contract_module, stub):
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, model_grants())

    _leader_result, validator = capture_validator(m, stub, spec)
    assert validator(stub.Rollback(f"{m.ERROR_TRANSIENT} fabricated outage")) is False


def test_model_faults_are_never_agreement(contract_module, stub):
    """An LLM_ERROR must rotate the leader, never write a verdict to chain."""
    m = contract_module
    spec = make_spec()
    serve(stub, {spec["evidence_urls"][0]: (200, LICENCE_PAGE)})
    respond(stub, "not json at all")

    with pytest.raises(m.gl.vm.UserError):
        m._adjudicate_with_consensus(spec)
    validator = stub._VM.last_validator
    assert validator(stub.Rollback(f"{m.ERROR_LLM} Model response was not parseable JSON")) is False
