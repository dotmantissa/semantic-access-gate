"""
Live StudioNet: deployment, registration, authority and the money paths.

This suite proves the deployed contract behaves as built. It is deliberately a subset of
the deterministic checks, not a copy of them: every validation branch is already
exercised against the same code inside the real GenVM in tests/direct, and the Studio
network enforces a request budget that receipt polling consumes quickly. Re-running forty
validation cases here would spend that budget without adding signal, so what runs live is
the deployment itself, the authority boundaries, and every path that moves money.

Each write goes through the `send` helper, which fails the test if no leader attempt
succeeded. A StudioNet transaction can report ACCEPTED while the call inside it raised,
so checking the status name alone would let real failures pass.
"""

import json

import pytest

from conftest import (
    CONSUMER_GATE_CONDITIONS,
    CONSUMER_GATE_DISQUALIFIERS,
    CONSUMER_GATE_POLICY,
    EVIDENCE_HOST_ECHO,
    EVIDENCE_HOST_REAL,
    LIVE_BOND,
    LIVE_STAKE,
    LIVE_TTL,
    MISSING_URL,
    README_URL,
    apply_and_get_id,
    echo_url,
    ensure_gate,
    expect_revert,
    read,
    read_json,
    send,
    unique_gate_id,
)

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


def test_registry_is_deployed_and_responding(owner, owner_account, registry_address):
    stats = read_json(owner, registry_address, "get_registry_stats", [])
    assert stats is not None
    assert stats["registry_owner"].lower() == owner_account.address.lower()
    assert int(stats["gate_count"]) >= 1
    assert int(stats["locked_wei"]) >= 0
    assert int(stats["treasury_wei"]) >= 0


def test_deployed_schema_exposes_the_full_interface(owner, registry_address):
    """
    The ABI a builder integrates against. is_approved is the composability surface, so it
    has to be present and readonly on the live deployment, not merely in the source.
    """
    methods = (owner.get_contract_schema(registry_address).get("methods") or {})

    writes = (
        "register_gate", "update_policy", "update_gate_config", "set_gate_paused",
        "transfer_gate_ownership", "withdraw_treasury", "apply_for_access",
        "adjudicate", "claim_stale_application", "release_access", "revoke_access",
        "challenge_access", "resolve_challenge",
    )
    views = (
        "is_approved", "access_status", "binding_token", "can_apply",
        "evidence_host_allowed", "get_gate", "get_policy", "get_application",
        "get_access_record", "get_access_history", "get_challenge", "get_rules",
        "list_gates", "list_applications", "list_holders",
        "get_applicant_applications", "gate_stats", "get_registry_stats",
    )

    for name in writes:
        assert name in methods, f"missing write method {name}"
        assert methods[name]["readonly"] is False, f"{name} should not be a view"
    for name in views:
        assert name in methods, f"missing view method {name}"
        assert methods[name]["readonly"] is True, f"{name} should be a view"

    assert len(methods) == len(writes) + len(views)
    assert methods["apply_for_access"]["payable"] is True
    assert methods["challenge_access"]["payable"] is True
    assert methods["adjudicate"]["payable"] is False


def test_consumer_is_deployed_and_pinned_to_the_registry(
    owner, owner_account, consumer_address, registry_address, deployment
):
    info = read_json(owner, consumer_address, "gate_info", [])
    assert info["gate_registry"].lower() == registry_address.lower()
    assert info["gate_id"] == deployment["consumerBoundGateId"]
    assert info["expected_gate_owner"].lower() == owner_account.address.lower()


# ---------------------------------------------------------------------------
# Registered gates
# ---------------------------------------------------------------------------


def test_the_gate_stores_its_policy_verbatim(owner, owner_account, registry_address, consumer_gate):
    gate = read_json(owner, registry_address, "get_gate", [consumer_gate])
    assert gate["gate_id"] == consumer_gate
    assert gate["owner"].lower() == owner_account.address.lower()
    assert gate["policy_text"] == CONSUMER_GATE_POLICY
    assert [c["id"] for c in gate["conditions"]] == sorted(
        c["id"] for c in CONSUMER_GATE_CONDITIONS
    )
    assert [d["id"] for d in gate["disqualifiers"]] == [
        d["id"] for d in CONSUMER_GATE_DISQUALIFIERS
    ]
    assert gate["allowed_hosts"] == [EVIDENCE_HOST_REAL]
    assert gate["fetch_mode"] == "raw"
    assert gate["require_grounded_quotes"] is True
    assert int(gate["policy_version"]) >= 1
    assert int(gate["rules_version"]) >= 1
    assert int(gate["access_ttl_seconds"]) == LIVE_TTL
    assert gate["bond_wei"] == str(LIVE_BOND)
    assert gate["challenge_stake_wei"] == str(LIVE_STAKE)

    # get_policy must agree with get_gate: they are what validators adjudicate against.
    # The whole eligibility frame is compared, not only the prose, because the
    # mechanical requirements decide who qualifies just as directly as the text does.
    policy = read_json(owner, registry_address, "get_policy", [consumer_gate])
    assert policy["policy_text"] == gate["policy_text"]
    assert policy["policy_version"] == gate["policy_version"]
    assert policy["rules_version"] == gate["rules_version"]
    assert policy["conditions"] == gate["conditions"]
    assert policy["disqualifiers"] == gate["disqualifiers"]
    assert policy["allowed_hosts"] == gate["allowed_hosts"]
    assert policy["fetch_mode"] == gate["fetch_mode"]
    assert policy["binding_required"] == gate["binding_required"]
    assert policy["require_grounded_quotes"] == gate["require_grounded_quotes"]

    # The frame the gate is currently on must be published and byte-identical to it,
    # because that published snapshot is what adjudications actually read.
    frame = read_json(
        owner, registry_address, "get_rules",
        [consumer_gate, int(gate["policy_version"]), int(gate["rules_version"])],
    )
    assert frame["gate_id"] == consumer_gate
    assert frame["policy_text"] == gate["policy_text"]
    assert frame["conditions"] == gate["conditions"]
    assert frame["disqualifiers"] == gate["disqualifiers"]
    assert frame["allowed_hosts"] == gate["allowed_hosts"]
    assert frame["fetch_mode"] == gate["fetch_mode"]
    assert frame["binding_required"] == gate["binding_required"]
    assert frame["require_grounded_quotes"] == gate["require_grounded_quotes"]


def test_the_wallet_bound_gate_requires_proof_of_control(owner, registry_address, bound_gate):
    gate = read_json(owner, registry_address, "get_gate", [bound_gate])
    assert gate["binding_required"] is True
    assert gate["require_grounded_quotes"] is True
    assert gate["allowed_hosts"] == [EVIDENCE_HOST_ECHO]


def test_gates_are_indexed_in_the_registry(owner, registry_address, consumer_gate, bound_gate):
    listing = read_json(owner, registry_address, "list_gates", [0, 200])
    assert consumer_gate in listing["gate_ids"]
    assert bound_gate in listing["gate_ids"]
    assert listing["total"] == len(listing["gate_ids"]) or listing["total"] > 0


def test_unknown_objects_read_as_empty_rather_than_reverting(owner, registry_address):
    """A consumer front end should never have to catch a revert on a read."""
    assert read(owner, registry_address, "get_gate", ["definitely-not-a-gate"]) == ""
    assert read(owner, registry_address, "get_application", ["app_999999"]) == ""
    assert read(owner, registry_address, "get_challenge", ["chal_999999"]) == ""
    assert read(owner, registry_address, "is_approved",
                ["definitely-not-a-gate", owner.local_account.address]) is False
    assert read_json(owner, registry_address, "can_apply",
                     ["definitely-not-a-gate", owner.local_account.address]) == {
        "eligible": False, "reason": "UNKNOWN_GATE",
    }


# ---------------------------------------------------------------------------
# Preflight views
# ---------------------------------------------------------------------------


def test_preflight_and_binding_token_views(owner, other, registry_address, bound_gate):
    mine = read(owner, registry_address, "binding_token",
                [bound_gate, owner.local_account.address])
    theirs = read(owner, registry_address, "binding_token",
                  [bound_gate, other.local_account.address])

    assert mine.startswith("glgate:" + bound_gate + ":")
    assert owner.local_account.address.lower() in mine
    assert mine != theirs, "a token that did not name its address would be transferable"

    pre = read_json(owner, registry_address, "can_apply",
                    [bound_gate, owner.local_account.address])
    assert pre["required_bond_wei"] == str(LIVE_BOND)
    assert pre["binding_required"] is True
    assert pre["binding_token"] == mine
    assert pre["reason"] in (
        "OK", "ALREADY_APPROVED", "APPLICATION_PENDING", "COOLDOWN_ACTIVE",
        "CHALLENGE_OPEN",
    )


def test_the_host_allowlist_view_matches_the_gate(owner, registry_address, consumer_gate):
    checks = {
        README_URL: True,
        "https://raw.githubusercontent.com/any/other/file.md": True,
        "https://sub.raw.githubusercontent.com/x": True,
        "https://raw.githubusercontent.com.evil.example/x": False,
        "https://evilraw.githubusercontent.com.attacker.net/x": False,
        echo_url("unrelated host"): False,
        "http://raw.githubusercontent.com/x": False,
        "not-even-a-url": False,
    }
    for url, expected in checks.items():
        actual = read(owner, registry_address, "evidence_host_allowed", [consumer_gate, url])
        assert actual is expected, f"{url} should be {expected}"


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def test_a_non_owner_cannot_rewrite_a_policy(other, registry_address, consumer_gate):
    """
    Permissionless registration is not permissionless control. A second identity can
    register gates of its own and cannot touch this one. This is the boundary that a
    consumer's trust in `gate.owner` rests on.
    """
    failure = expect_revert(
        other, registry_address, "update_policy",
        [
            consumer_gate,
            CONSUMER_GATE_POLICY + " An unauthorised addition to the policy text.",
            json.dumps(CONSUMER_GATE_CONDITIONS),
            json.dumps(CONSUMER_GATE_DISQUALIFIERS),
        ],
    )
    assert "Only the gate owner" in failure, failure


def test_a_duplicate_gate_id_is_refused_on_chain(owner, registry_address, consumer_gate):
    failure = expect_revert(
        owner, registry_address, "register_gate",
        [
            consumer_gate, "Duplicate attempt", CONSUMER_GATE_POLICY,
            json.dumps(CONSUMER_GATE_CONDITIONS), json.dumps([]), json.dumps([]),
            "raw", False, True, LIVE_TTL, 0, 0, 0,
        ],
    )
    assert "already registered" in failure, failure


def test_evidence_on_a_disallowed_host_is_refused_on_chain(owner, registry_address):
    """
    The allowlist is enforced before a bond is taken, so an applicant cannot pay to have
    evidence they host themselves adjudicated against a gate that does not accept it.

    This runs against its own gate rather than the consumer gate. Intake checks a live
    access record before it validates the evidence URLs, so asserting the host refusal
    on a gate this address might already hold access to would assert the wrong guard
    depending on which other tests had run first.
    """
    gate_id = unique_gate_id("live-host-refusal")
    ensure_gate(
        owner, registry_address, gate_id,
        title="Host Allowlist Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=LIVE_BOND,
    )
    assert read_json(owner, registry_address, "can_apply",
                     [gate_id, owner.local_account.address])["eligible"] is True

    failure = expect_revert(
        owner, registry_address, "apply_for_access",
        [gate_id, json.dumps(["https://pastebin.example.net/forged"]), "note"],
        value=LIVE_BOND,
    )
    assert "Host not permitted" in failure, failure

    # And the mixed case: one permitted URL does not carry a disallowed one in with it.
    failure = expect_revert(
        owner, registry_address, "apply_for_access",
        [gate_id, json.dumps([README_URL, "https://pastebin.example.net/forged"]), "note"],
        value=LIVE_BOND,
    )
    assert "Host not permitted" in failure, failure


# ---------------------------------------------------------------------------
# The money paths
# ---------------------------------------------------------------------------


def test_a_dead_link_denies_and_the_treasury_can_be_drawn_down(owner, registry_address):
    """
    A complete live round with no model involved, and the money followed both ways.

    The evidence URL is on an allowed host and returns 404, which every validator sees
    identically, so the adjudication resolves to a denial rather than an error. The bond
    is forfeited to the gate treasury, the owner cannot withdraw more than that, and can
    withdraw exactly that.
    """
    gate_id = "live-money-gate"
    ensure_gate(
        owner, registry_address, gate_id,
        title="Dead Link Denial Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=LIVE_BOND,
        challenge_stake_wei=LIVE_STAKE,
    )

    treasury_before = int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"])
    locked_before = int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"])

    app_id = apply_and_get_id(
        owner, registry_address, gate_id, [MISSING_URL],
        "The evidence page has been removed.", LIVE_BOND,
    )
    pending = read_json(owner, registry_address, "get_application", [app_id])
    assert pending["status"] == "PENDING"
    assert pending["bond_wei"] == str(LIVE_BOND)
    assert int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]) == (
        locked_before + LIVE_BOND
    )

    send(owner, registry_address, "adjudicate", [app_id])

    settled = read_json(owner, registry_address, "get_application", [app_id])
    assert settled["status"] == "DENIED"
    assert settled["denial_code"] == "EVIDENCE_UNRETRIEVABLE"
    assert settled["evidence_ok"] is False
    assert settled["evidence_status"][0]["status"] == 404
    assert settled["conditions"] == [], "the model is never consulted on a dead link"
    assert read(owner, registry_address, "is_approved",
                [gate_id, owner.local_account.address]) is False

    treasury_after = int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"])
    assert treasury_after == treasury_before + LIVE_BOND
    assert int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]) == (
        locked_before
    )

    failure = expect_revert(
        owner, registry_address, "withdraw_treasury", [gate_id, treasury_after + 1]
    )
    assert "exceeds gate treasury" in failure, failure

    send(owner, registry_address, "withdraw_treasury", [gate_id, treasury_after])
    assert int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"]) == 0


def test_a_policy_change_supersedes_a_pending_application_and_refunds_it(owner, registry_address):
    """
    The fairness path, live. An applicant who paid a bond against one set of conditions is
    refunded rather than judged against conditions they never saw. No evidence is fetched
    and no model is called, which is visible in the empty audit trail.
    """
    gate_id = "live-supersede-gate"
    ensure_gate(
        owner, registry_address, gate_id,
        title="Supersede Refund Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=LIVE_BOND,
    )

    app_id = apply_and_get_id(
        owner, registry_address, gate_id, [README_URL], "Filed under the earlier policy.", LIVE_BOND
    )
    filed_version = int(read_json(owner, registry_address, "get_application", [app_id])["policy_version"])
    locked_after_apply = int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"])

    send(
        owner, registry_address, "update_policy",
        [
            gate_id,
            CONSUMER_GATE_POLICY + " Applicants must additionally publish a signed attestation.",
            json.dumps(CONSUMER_GATE_CONDITIONS + [
                {"id": "c3_attestation",
                 "text": "The document includes a signed attestation of authorship"}
            ]),
            json.dumps([]),
        ],
    )
    new_version = int(read_json(owner, registry_address, "get_policy", [gate_id])["policy_version"])
    assert new_version == filed_version + 1

    send(owner, registry_address, "adjudicate", [app_id])

    settled = read_json(owner, registry_address, "get_application", [app_id])
    assert settled["status"] == "SUPERSEDED"
    assert settled["decision"] == ""
    assert settled["conditions"] == []
    assert f"version {filed_version}" in settled["reasoning"]
    assert f"version {new_version}" in settled["reasoning"]

    gate = read_json(owner, registry_address, "get_gate", [gate_id])
    assert gate["treasury_wei"] == "0", "a supersede is a refund, never revenue"
    assert int(gate["total_denied"]) == 0

    assert int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]) == (
        locked_after_apply - LIVE_BOND
    )
    assert read_json(owner, registry_address, "can_apply",
                     [gate_id, owner.local_account.address])["eligible"] is True


def test_a_rules_change_supersedes_a_pending_application_and_refunds_it(
    owner, registry_address
):
    """
    The same fairness guarantee as a policy change, for the mechanical half of the
    eligibility frame. The owner switches the wallet binding requirement on after the
    bond is posted; the application is refunded rather than judged against a check the
    applicant never agreed to.

    This direction matters most in the other sense. Had the owner been switching
    binding and quote grounding OFF, judging the application under the new frame would
    have removed the only two deterministic barriers between a document and a grant,
    letting the gate owner manufacture access the evidence never earned.
    """
    gate_id = unique_gate_id("live-rules-supersede")
    ensure_gate(
        owner, registry_address, gate_id,
        title="Rules Supersede Refund Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=LIVE_BOND,
    )

    app_id = apply_and_get_id(
        owner, registry_address, gate_id, [README_URL],
        "Filed under the earlier adjudication rules.", LIVE_BOND,
    )
    filed = read_json(owner, registry_address, "get_application", [app_id])
    filed_rules = int(filed["rules_version"])
    locked_after_apply = int(
        read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]
    )

    send(
        owner, registry_address, "update_gate_config",
        [
            gate_id, json.dumps([EVIDENCE_HOST_REAL]), "raw",
            True,   # binding_required switched on: an eligibility change
            True, LIVE_TTL, LIVE_BOND, LIVE_STAKE, 0,
        ],
    )
    gate = read_json(owner, registry_address, "get_gate", [gate_id])
    assert int(gate["rules_version"]) == filed_rules + 1
    assert int(gate["policy_version"]) == int(filed["policy_version"]), "policy untouched"

    send(owner, registry_address, "adjudicate", [app_id])

    settled = read_json(owner, registry_address, "get_application", [app_id])
    assert settled["status"] == "SUPERSEDED"
    assert settled["decision"] == ""
    assert settled["conditions"] == [], "no evidence was fetched and no model was called"
    assert f"rules changed from version {filed_rules}" in settled["reasoning"]

    assert gate["treasury_wei"] == "0", "a supersede is a refund, never revenue"
    assert int(gate["total_denied"]) == 0
    assert int(
        read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]
    ) == locked_after_apply - LIVE_BOND
    assert read_json(owner, registry_address, "can_apply",
                     [gate_id, owner.local_account.address])["eligible"] is True


def test_an_economic_change_moves_no_version(owner, registry_address):
    """
    The complement, and the reason the two groups of settings are separated. Repricing a
    gate must not invalidate its holders, so an edit that touches only the bond, the
    stake, the TTL and the cooldown moves neither counter.
    """
    gate_id = unique_gate_id("live-reprice")
    ensure_gate(
        owner, registry_address, gate_id,
        title="Repricing Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=LIVE_BOND,
    )
    before = read_json(owner, registry_address, "get_gate", [gate_id])

    send(
        owner, registry_address, "update_gate_config",
        [
            gate_id, json.dumps([EVIDENCE_HOST_REAL]), "raw", False, True,
            LIVE_TTL * 2, LIVE_BOND * 2, LIVE_STAKE * 2, 60,
        ],
    )
    after = read_json(owner, registry_address, "get_gate", [gate_id])

    assert int(after["rules_version"]) == int(before["rules_version"])
    assert int(after["policy_version"]) == int(before["policy_version"])
    assert after["bond_wei"] == str(LIVE_BOND * 2), "the reprice did take effect"
    assert int(after["access_ttl_seconds"]) == LIVE_TTL * 2


def test_a_published_frame_is_immutable_on_chain(owner, registry_address):
    """
    The snapshot a pending application is judged under has to be beyond the owner's
    reach. After flipping every adjudication-relevant setting the original version pair
    still reads back byte for byte, and the new pair reads back as the new rules.
    """
    gate_id = unique_gate_id("live-frame-immutable")
    ensure_gate(
        owner, registry_address, gate_id,
        title="Immutable Frame Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        require_grounded_quotes=True,
        bond_wei=0,
        challenge_stake_wei=0,
    )
    gate = read_json(owner, registry_address, "get_gate", [gate_id])
    pv, rv = int(gate["policy_version"]), int(gate["rules_version"])

    original = read_json(owner, registry_address, "get_rules", [gate_id, pv, rv])
    assert original["binding_required"] is False
    assert original["require_grounded_quotes"] is True
    assert original["fetch_mode"] == "raw"
    assert original["allowed_hosts"] == [EVIDENCE_HOST_REAL]

    send(
        owner, registry_address, "update_gate_config",
        [
            gate_id, json.dumps([EVIDENCE_HOST_ECHO]), "render", True, False,
            LIVE_TTL, 0, 0, 0,
        ],
    )

    assert read_json(owner, registry_address, "get_rules", [gate_id, pv, rv]) == original, (
        "a published frame was rewritten by the gate owner"
    )
    updated = read_json(owner, registry_address, "get_rules", [gate_id, pv, rv + 1])
    assert updated["binding_required"] is True
    assert updated["require_grounded_quotes"] is False
    assert updated["fetch_mode"] == "render"
    assert updated["allowed_hosts"] == [EVIDENCE_HOST_ECHO]

    assert read(owner, registry_address, "get_rules", [gate_id, 99, 99]) == ""


def test_owner_controls_intake_and_can_hand_over_the_gate(owner, other, registry_address):
    """
    Pausing stops intake only, and ownership moves cleanly. A gate is meant to outlive
    whoever registered it, which is what makes handing it to a DAO or a multisig the
    right answer to the trust question a consumer has to ask.
    """
    gate_id = "live-control-gate"
    ensure_gate(
        owner, registry_address, gate_id,
        title="Owner Controls Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        bond_wei=0,
        challenge_stake_wei=0,
    )

    send(owner, registry_address, "set_gate_paused", [gate_id, True])
    assert read_json(owner, registry_address, "get_gate", [gate_id])["paused"] is True
    assert read_json(owner, registry_address, "can_apply",
                     [gate_id, owner.local_account.address])["reason"] == "GATE_PAUSED"

    failure = expect_revert(
        owner, registry_address, "apply_for_access",
        [gate_id, json.dumps([README_URL]), "note"], value=0,
    )
    assert "paused" in failure, failure

    send(owner, registry_address, "set_gate_paused", [gate_id, False])
    assert read_json(owner, registry_address, "get_gate", [gate_id])["paused"] is False

    send(owner, registry_address, "transfer_gate_ownership", [gate_id, other.local_account.address])
    assert read_json(owner, registry_address, "get_gate", [gate_id])["owner"].lower() == (
        other.local_account.address.lower()
    )

    lost = expect_revert(owner, registry_address, "set_gate_paused", [gate_id, True])
    assert "Only the gate owner" in lost, lost

    send(other, registry_address, "transfer_gate_ownership", [gate_id, owner.local_account.address])
    assert read_json(owner, registry_address, "get_gate", [gate_id])["owner"].lower() == (
        owner.local_account.address.lower()
    )


def test_registry_totals_match_the_gate_treasuries(owner, registry_address):
    """
    A cross check against whatever live state this deployment has accumulated: the
    registry's treasury total must equal the sum of every gate's treasury.
    """
    stats = read_json(owner, registry_address, "get_registry_stats", [])
    listing = read_json(owner, registry_address, "list_gates", [0, 200])
    summed = sum(
        int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"])
        for gate_id in listing["gate_ids"]
    )
    assert int(stats["treasury_wei"]) == summed
    assert int(stats["gate_count"]) == listing["total"]
