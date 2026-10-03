"""
Deterministic tests for the consensus-critical pure functions.

Everything exercised here is the exact code that ships in the contract. These
functions decide whether an address gets access, so each one is tested across its
full input space rather than on a happy path.
"""

import json

import pytest


# ---------------------------------------------------------------------------
# _reduce_decision: the function every node independently applies
# ---------------------------------------------------------------------------


def sat(cid):
    return {"id": cid, "verdict": "SATISFIED", "quote": "q", "note": ""}


def unsat(cid):
    return {"id": cid, "verdict": "NOT_SATISFIED", "quote": "", "note": ""}


def disq(did, present):
    return {"id": did, "present": present, "note": ""}


def test_reduce_grants_only_when_every_condition_is_satisfied(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision(
        [sat("c1"), sat("c2"), sat("c3")], [], True, True
    )
    assert decision == m.DECISION_GRANTED
    assert code == m.DENIAL_NONE
    assert failed == []


def test_reduce_denies_when_any_single_condition_fails(contract_module):
    m = contract_module
    for failing in range(3):
        conditions = [sat("c1"), sat("c2"), sat("c3")]
        conditions[failing] = unsat(f"c{failing + 1}")
        decision, code, failed = m._reduce_decision(conditions, [], True, True)
        assert decision == m.DECISION_DENIED
        assert code == m.DENIAL_CONDITIONS
        assert failed == [f"c{failing + 1}"]


def test_reduce_reports_every_failing_condition(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision(
        [unsat("c1"), sat("c2"), unsat("c3")], [], True, True
    )
    assert decision == m.DECISION_DENIED
    assert failed == ["c1", "c3"]


def test_reduce_denies_when_a_disqualifier_is_present(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision(
        [sat("c1"), sat("c2")], [disq("d1", False), disq("d2", True)], True, True
    )
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_DISQUALIFIED
    assert failed == ["d2"]


def test_disqualifier_outranks_fully_satisfied_conditions(contract_module):
    """A sanction beats a perfect record. Order of checks is part of the policy."""
    m = contract_module
    decision, code, _ = m._reduce_decision(
        [sat("c1")], [disq("d1", True)], True, True
    )
    assert (decision, code) == (m.DECISION_DENIED, m.DENIAL_DISQUALIFIED)


def test_binding_failure_short_circuits_everything(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision(
        [sat("c1")], [disq("d1", True)], False, True
    )
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_BINDING
    assert failed == []


def test_unretrievable_evidence_outranks_condition_findings(contract_module):
    m = contract_module
    decision, code, _ = m._reduce_decision([sat("c1")], [], True, False)
    assert (decision, code) == (m.DECISION_DENIED, m.DENIAL_UNRETRIEVABLE)


def test_empty_condition_set_can_never_grant(contract_module):
    """Defensive floor: a gate with no conditions must not become an open door."""
    m = contract_module
    decision, code, _ = m._reduce_decision([], [], True, True)
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_CONDITIONS


def test_malformed_condition_entry_is_treated_as_failure(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision(
        [sat("c1"), "not-a-dict"], [], True, True
    )
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_CONDITIONS


def test_missing_verdict_key_is_treated_as_failure(contract_module):
    m = contract_module
    decision, _, failed = m._reduce_decision([{"id": "c1"}], [], True, True)
    assert decision == m.DECISION_DENIED
    assert failed == ["c1"]


def test_reduce_is_deterministic_over_repeated_calls(contract_module):
    m = contract_module
    args = ([sat("c1"), unsat("c2")], [disq("d1", False)], True, True)
    first = m._reduce_decision(*args)
    for _ in range(50):
        assert m._reduce_decision(*args) == first


def test_and_reduction_property_exhaustively(contract_module):
    """
    The security argument for comparing one decision field instead of every
    condition: GRANTED is reachable only when the conjunction holds. Enumerated
    over all 2^4 condition patterns crossed with both disqualifier states.
    """
    m = contract_module
    for mask in range(16):
        conditions = [
            sat(f"c{i}") if (mask >> i) & 1 else unsat(f"c{i}") for i in range(4)
        ]
        all_satisfied = mask == 15
        for disq_present in (False, True):
            decision, _, _ = m._reduce_decision(
                conditions, [disq("d1", disq_present)], True, True
            )
            expected = (
                m.DECISION_GRANTED
                if (all_satisfied and not disq_present)
                else m.DECISION_DENIED
            )
            assert decision == expected


# ---------------------------------------------------------------------------
# Evidence admissibility: the host allowlist as an adjudication rule
# ---------------------------------------------------------------------------
# The allowlist is part of the frozen frame an application is judged under, not just
# an intake filter, so the reducer has to treat an inadmissible document set as a
# denial in its own right. Because every public path stamps the frame and supersedes
# an application whose frame moved, this branch is unreachable from outside the
# contract - which is exactly why it is pinned here rather than left untested.


def test_inadmissible_evidence_denies_with_its_own_code(contract_module):
    m = contract_module
    decision, code, failed = m._reduce_decision([sat("c1")], [], True, True, False)
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_HOST_NOT_ALLOWED
    assert failed == []


def test_admissibility_outranks_every_other_finding(contract_module):
    """
    A document from a host the frame does not permit is not evidence, so nothing read
    out of it - not a satisfied condition, not a binding match - can outweigh that.
    """
    m = contract_module
    for binding_ok in (True, False):
        for evidence_ok in (True, False):
            decision, code, _ = m._reduce_decision(
                [sat("c1")], [disq("d1", True)], binding_ok, evidence_ok, False
            )
            assert (decision, code) == (m.DECISION_DENIED, m.DENIAL_HOST_NOT_ALLOWED)


def test_admissible_evidence_leaves_the_reduction_unchanged(contract_module):
    """Passing admissibility must be a no-op, not a new way to grant."""
    m = contract_module
    assert m._reduce_decision([sat("c1")], [], True, True, True) == m._reduce_decision(
        [sat("c1")], [], True, True
    )
    assert m._reduce_decision([unsat("c1")], [], True, True, True) == m._reduce_decision(
        [unsat("c1")], [], True, True
    )


def test_admissibility_defaults_to_permitted_for_older_payloads(contract_module):
    """
    The parameter defaults to True so a leader payload that omits hosts_ok re-reduces
    the same way a validator's own run would, rather than flipping to a denial on a
    missing key.
    """
    m = contract_module
    decision, _code, _failed = m._reduce_decision([sat("c1")], [], True, True)
    assert decision == m.DECISION_GRANTED


def test_an_inadmissible_document_is_never_fetched(contract_module, stub):
    """
    The point of checking admissibility before retrieval: a disallowed host must not
    be contacted at all, so narrowing an allowlist cannot turn into a fetch the frame
    never authorized.
    """
    m = contract_module
    contacted = []

    def handler(url):
        contacted.append(url)
        return stub.WebResponse(200, b"credential evidence")

    stub._Web.get_handler = handler
    docs = m._fetch_evidence(
        ["https://registry.example.com/ok", "https://elsewhere.example.net/bad"],
        "raw",
        ["registry.example.com"],
    )

    assert contacted == ["https://registry.example.com/ok"]
    assert [d["admissible"] for d in docs] == [True, False]
    assert docs[1]["retrievable"] is False
    assert docs[1]["text"] == ""


def test_an_empty_allowlist_admits_every_document(contract_module, stub):
    m = contract_module
    stub._Web.get_handler = lambda url: stub.WebResponse(200, b"evidence")
    docs = m._fetch_evidence(["https://anything.example.org/x"], "raw", [])
    assert docs[0]["admissible"] is True
    assert docs[0]["retrievable"] is True


def test_a_subdomain_of_an_allowed_host_is_admissible(contract_module, stub):
    m = contract_module
    stub._Web.get_handler = lambda url: stub.WebResponse(200, b"evidence")
    docs = m._fetch_evidence(
        ["https://sub.registry.example.com/x"], "raw", ["registry.example.com"]
    )
    assert docs[0]["admissible"] is True


def test_a_lookalike_host_is_inadmissible(contract_module, stub):
    m = contract_module
    stub._Web.get_handler = lambda url: stub.WebResponse(200, b"evidence")
    docs = m._fetch_evidence(
        ["https://evilregistry.example.com/x"], "raw", ["registry.example.com"]
    )
    assert docs[0]["admissible"] is False


# ---------------------------------------------------------------------------
# Host parsing and allowlisting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/alice", "github.com"),
        ("https://GitHub.COM/Alice", "github.com"),
        ("https://github.com", "github.com"),
        ("https://github.com:443/path", "github.com"),
        ("https://user@github.com/path", "github.com"),
        ("https://user:pw@github.com:8443/path?q=1#frag", "github.com"),
        ("https://sub.domain.github.com/x", "sub.domain.github.com"),
        ("https://github.com?q=1", "github.com"),
        ("https://github.com#frag", "github.com"),
        ("  https://github.com/x  ", "github.com"),
        ("http://github.com/x", ""),
        ("ftp://github.com/x", ""),
        ("github.com/x", ""),
        ("", ""),
    ],
)
def test_host_extraction(contract_module, url, expected):
    assert contract_module._host_of(url) == expected


def test_empty_allowlist_permits_any_host(contract_module):
    assert contract_module._host_allowed("anything.example", []) is True


def test_allowlist_matches_exact_host(contract_module):
    assert contract_module._host_allowed("github.com", ["github.com"]) is True


def test_allowlist_matches_subdomains(contract_module):
    assert contract_module._host_allowed("gist.github.com", ["github.com"]) is True
    assert (
        contract_module._host_allowed("a.b.c.github.com", ["github.com"]) is True
    )


def test_allowlist_rejects_suffix_lookalike_domains(contract_module):
    """The classic bypass: evilgithub.com must never satisfy an entry of github.com."""
    m = contract_module
    assert m._host_allowed("evilgithub.com", ["github.com"]) is False
    assert m._host_allowed("github.com.evil.example", ["github.com"]) is False
    assert m._host_allowed("notgithub.com", ["github.com"]) is False


def test_allowlist_rejects_unlisted_host(contract_module):
    assert contract_module._host_allowed("example.com", ["github.com"]) is False


def test_allowlist_handles_leading_dot_entries(contract_module):
    assert contract_module._host_allowed("gist.github.com", [".github.com"]) is True


def test_allowlist_rejects_empty_host_when_restricted(contract_module):
    assert contract_module._host_allowed("", ["github.com"]) is False


# ---------------------------------------------------------------------------
# Binding token
# ---------------------------------------------------------------------------


def test_binding_token_embeds_the_applicant_address(contract_module):
    token = contract_module._binding_token("med-gate", "0xAbCd1234")
    assert token == "glgate:med-gate:0xabcd1234"


def test_binding_token_is_case_insensitive_on_the_address(contract_module):
    m = contract_module
    assert m._binding_token("g", "0xABC") == m._binding_token("g", "0xabc")


def test_binding_tokens_differ_across_addresses(contract_module):
    """
    Republishing someone else's token is worthless: the token names the address it
    authorises, so an attacker applying from their own address needs their own.
    """
    m = contract_module
    assert m._binding_token("g", "0xaaa") != m._binding_token("g", "0xbbb")


def test_binding_tokens_differ_across_gates(contract_module):
    m = contract_module
    assert m._binding_token("gate-a", "0xaaa") != m._binding_token("gate-b", "0xaaa")


# ---------------------------------------------------------------------------
# Text normalization, verdicts, flags
# ---------------------------------------------------------------------------


def test_normalize_text_collapses_punctuation_and_whitespace(contract_module):
    m = contract_module
    assert m._normalize_text("Dr.  Jane   Doe, M.D.") == "dr jane doe m d"


def test_normalize_text_is_case_insensitive(contract_module):
    m = contract_module
    assert m._normalize_text("HELLO World") == m._normalize_text("hello world")


def test_normalize_text_survives_reflowed_whitespace(contract_module):
    """A quote must still match when the model rewraps newlines into spaces."""
    m = contract_module
    assert m._normalize_text("line one\nline two") == m._normalize_text(
        "line one   line two"
    )


@pytest.mark.parametrize(
    "value", ["SATISFIED", "satisfied", " Satisfied ", "PASS", "yes", "true", "met"]
)
def test_verdicts_that_mean_satisfied(contract_module, value):
    assert contract_module._normalize_verdict(value) == contract_module.VERDICT_SATISFIED


@pytest.mark.parametrize(
    "value", ["NOT_SATISFIED", "not satisfied", "NOT-SATISFIED", "no", "", "maybe", None, 7]
)
def test_everything_else_is_not_satisfied(contract_module, value):
    m = contract_module
    assert m._normalize_verdict(value) == m.VERDICT_NOT_SATISFIED


@pytest.mark.parametrize("value", [True, 1, "true", "YES", "present", "detected", "1"])
def test_flags_that_coerce_true(contract_module, value):
    assert contract_module._coerce_flag(value) is True


@pytest.mark.parametrize("value", [False, 0, "false", "no", "", None, "unknown"])
def test_flags_that_coerce_false(contract_module, value):
    assert contract_module._coerce_flag(value) is False


# ---------------------------------------------------------------------------
# Model response parsing
# ---------------------------------------------------------------------------


def test_clean_json_passes_through_a_dict(contract_module):
    assert contract_module._clean_json({"a": 1}) == {"a": 1}


def test_clean_json_extracts_from_surrounding_prose(contract_module):
    out = contract_module._clean_json('Sure! Here it is: {"a": 1} Hope that helps.')
    assert out == {"a": 1}


def test_clean_json_extracts_from_a_markdown_fence(contract_module):
    out = contract_module._clean_json('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_clean_json_tolerates_a_trailing_comma(contract_module):
    out = contract_module._clean_json('{"a": 1, "b": 2,}')
    assert out == {"a": 1, "b": 2}


def test_clean_json_handles_nested_objects(contract_module):
    payload = {"conditions": [{"id": "c1", "verdict": "SATISFIED"}], "reasoning": "ok"}
    assert contract_module._clean_json(json.dumps(payload)) == payload


def test_unparseable_response_raises_a_classified_llm_error(contract_module):
    m = contract_module
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._clean_json("the model refused to answer")
    assert exc.value.message.startswith(m.ERROR_LLM)


def test_json_array_response_raises_llm_error(contract_module):
    m = contract_module
    with pytest.raises(m.gl.vm.UserError) as exc:
        m._clean_json("[1, 2, 3]")
    assert exc.value.message.startswith(m.ERROR_LLM)


# ---------------------------------------------------------------------------
# Denial reason precedence
# ---------------------------------------------------------------------------


def test_unretrievable_evidence_is_reported_ahead_of_a_missing_token(contract_module):
    """
    When nothing was fetched the binding token cannot be present either. The
    applicant needs to hear about the dead link, which is the fault they can fix,
    rather than a token check that never had bytes to run against.
    """
    m = contract_module
    decision, code, _ = m._reduce_decision([], [], False, False)
    assert decision == m.DECISION_DENIED
    assert code == m.DENIAL_UNRETRIEVABLE


def test_binding_is_reported_when_evidence_was_retrievable(contract_module):
    m = contract_module
    decision, code, _ = m._reduce_decision([sat("c1")], [], False, True)
    assert (decision, code) == (m.DECISION_DENIED, m.DENIAL_BINDING)


def test_denial_precedence_is_total_and_stable(contract_module):
    """Every combination of the four gating inputs maps to exactly one reason code."""
    m = contract_module
    expected = {
        # (evidence_ok, binding_ok, disqualifier_present, all_conditions_met)
        (False, False, False, False): m.DENIAL_UNRETRIEVABLE,
        (False, True, True, True): m.DENIAL_UNRETRIEVABLE,
        (True, False, True, True): m.DENIAL_BINDING,
        (True, False, False, False): m.DENIAL_BINDING,
        (True, True, True, True): m.DENIAL_DISQUALIFIED,
        (True, True, True, False): m.DENIAL_DISQUALIFIED,
        (True, True, False, False): m.DENIAL_CONDITIONS,
        (True, True, False, True): m.DENIAL_NONE,
    }
    for (ev, bind, dq, met), code in expected.items():
        conditions = [sat("c1")] if met else [unsat("c1")]
        decision, actual, _ = m._reduce_decision(
            conditions, [disq("d1", dq)], bind, ev
        )
        assert actual == code, (ev, bind, dq, met)
        assert decision == (
            m.DECISION_GRANTED if code == m.DENIAL_NONE else m.DECISION_DENIED
        )
