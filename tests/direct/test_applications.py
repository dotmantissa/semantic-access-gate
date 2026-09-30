"""
Application intake: bonds, evidence validation, eligibility and the stale bond path.

Intake is where money enters the contract, so every rejection here is a rejection
before a bond is taken, and every accepted application is accounted for.
"""

import json

import pytest

from conftest import (
    BOND,
    COOLDOWN,
    EVIDENCE_URL,
    GATE_ID,
    SECOND_URL,
    apply_as,
    hex_of,
    warp_time,
)


def test_application_records_every_field(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    assert app_id == "app_0"

    app = json.loads(gate.get_application(app_id))
    assert app["gate_id"] == GATE_ID
    assert app["applicant"].lower() == hex_of(direct_alice)
    assert app["evidence_urls"] == [EVIDENCE_URL]
    assert app["bond_wei"] == str(BOND)
    assert app["policy_version"] == 1
    assert app["status"] == "PENDING"
    assert app["decision"] == ""
    assert app["binding_ok"] is False
    assert app["evidence_ok"] is False


def test_bond_is_escrowed_and_counted_as_locked(gate, direct_vm, direct_alice):
    before = json.loads(gate.get_registry_stats())
    apply_as(gate, direct_vm, direct_alice)
    after = json.loads(gate.get_registry_stats())
    assert int(after["locked_wei"]) == int(before["locked_wei"]) + BOND
    assert after["treasury_wei"] == "0", "an unadjudicated bond is not revenue"


def test_exact_bond_is_required(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND - 1
    try:
        with direct_vm.expect_revert("Bond must be exactly"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "note")
    finally:
        direct_vm.value = 0


def test_overpaying_the_bond_is_rejected(gate, direct_vm, direct_alice):
    """
    An overpayment would leave change with no owner. Rejecting is cleaner than
    inventing a refund path for a mistake the caller can simply not make.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = BOND * 2
    try:
        with direct_vm.expect_revert("Bond must be exactly"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "note")
    finally:
        direct_vm.value = 0


def test_application_to_an_unknown_gate_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("Unknown gate"):
            gate.apply_for_access("no-such-gate", json.dumps([EVIDENCE_URL]), "note")
    finally:
        direct_vm.value = 0


def test_paused_gate_refuses_new_applications(gate, direct_vm, direct_owner, direct_alice):
    direct_vm.sender = direct_owner
    gate.set_gate_paused(GATE_ID, True)
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("paused"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "note")
    finally:
        direct_vm.value = 0


def test_second_application_while_one_is_pending_is_rejected(gate, direct_vm, direct_alice):
    """One open application per address per gate, so a bond cannot be double counted."""
    apply_as(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("already pending"):
            gate.apply_for_access(GATE_ID, json.dumps([EVIDENCE_URL]), "note")
    finally:
        direct_vm.value = 0


def test_different_addresses_can_apply_concurrently(gate, direct_vm, direct_alice, direct_bob):
    a = apply_as(gate, direct_vm, direct_alice)
    b = apply_as(gate, direct_vm, direct_bob)
    assert a != b
    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == BOND * 2
    assert stats["application_count"] == 2


def test_the_same_address_can_apply_to_different_gates(gate, direct_vm, direct_owner, direct_alice):
    from conftest import register_default_gate

    register_default_gate(gate, direct_vm, direct_owner, gate_id="second-gate")
    a = apply_as(gate, direct_vm, direct_alice)
    b = apply_as(gate, direct_vm, direct_alice, gate_id="second-gate")
    assert json.loads(gate.get_application(a))["gate_id"] == GATE_ID
    assert json.loads(gate.get_application(b))["gate_id"] == "second-gate"


# ---------------------------------------------------------------------------
# Evidence URL validation
# ---------------------------------------------------------------------------


def test_http_evidence_is_rejected(gate, direct_vm, direct_alice):
    """Plain HTTP is not verifiable evidence: any node's view of it can be tampered."""
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("must use https"):
            gate.apply_for_access(
                GATE_ID, json.dumps(["http://registry.example.com/x"]), "note"
            )
    finally:
        direct_vm.value = 0


def test_host_outside_the_allowlist_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("Host not permitted"):
            gate.apply_for_access(
                GATE_ID, json.dumps(["https://pastebin.example.net/fake"]), "note"
            )
    finally:
        direct_vm.value = 0


def test_lookalike_host_is_rejected(gate, direct_vm, direct_alice):
    """registry.example.com.evil.net must not satisfy an allowlist of registry.example.com."""
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("Host not permitted"):
            gate.apply_for_access(
                GATE_ID,
                json.dumps(["https://registry.example.com.evil.net/x"]),
                "note",
            )
    finally:
        direct_vm.value = 0


def test_subdomain_of_an_allowed_host_is_accepted(gate, direct_vm, direct_alice):
    app_id = apply_as(
        gate, direct_vm, direct_alice, urls=["https://sub.registry.example.com/x"]
    )
    assert json.loads(gate.get_application(app_id))["evidence_urls"] == [
        "https://sub.registry.example.com/x"
    ]


def test_empty_evidence_list_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("between 1 and"):
            gate.apply_for_access(GATE_ID, json.dumps([]), "note")
    finally:
        direct_vm.value = 0


def test_too_many_evidence_urls_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    urls = [f"https://registry.example.com/{i}" for i in range(5)]
    try:
        with direct_vm.expect_revert("between 1 and"):
            gate.apply_for_access(GATE_ID, json.dumps(urls), "note")
    finally:
        direct_vm.value = 0


def test_duplicate_urls_are_collapsed(gate, direct_vm, direct_alice):
    """
    Duplicates would be fetched twice and double weighted in the prompt for no gain.
    """
    app_id = apply_as(
        gate, direct_vm, direct_alice, urls=[EVIDENCE_URL, EVIDENCE_URL, SECOND_URL]
    )
    assert json.loads(gate.get_application(app_id))["evidence_urls"] == [
        EVIDENCE_URL,
        SECOND_URL,
    ]


def test_multiple_distinct_urls_are_kept_in_order(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice, urls=[SECOND_URL, EVIDENCE_URL])
    assert json.loads(gate.get_application(app_id))["evidence_urls"] == [
        SECOND_URL,
        EVIDENCE_URL,
    ]


def test_malformed_evidence_json_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    try:
        with direct_vm.expect_revert("valid JSON"):
            gate.apply_for_access(GATE_ID, "[[[", "note")
    finally:
        direct_vm.value = 0


def test_gate_with_an_open_allowlist_accepts_any_https_host(
    registry, direct_vm, direct_owner, direct_alice, funded
):
    from conftest import register_default_gate

    register_default_gate(registry, direct_vm, direct_owner, gate_id="open-gate", hosts=[])
    app_id = apply_as(
        registry, direct_vm, direct_alice,
        urls=["https://anything.at.all.example/page"], gate_id="open-gate",
    )
    assert json.loads(registry.get_application(app_id))["status"] == "PENDING"


def test_applicant_note_is_normalized_and_capped(gate, direct_vm, direct_alice):
    app_id = apply_as(
        gate, direct_vm, direct_alice, note="  lots   of \n\n whitespace  " + "x" * 800
    )
    note = json.loads(gate.get_application(app_id))["applicant_note"]
    assert note.startswith("lots of whitespace")
    assert len(note) <= 500


# ---------------------------------------------------------------------------
# Preflight and helper views
# ---------------------------------------------------------------------------


def test_preflight_reports_eligible_for_a_fresh_address(gate, direct_alice):
    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is True
    assert pre["reason"] == "OK"
    assert pre["required_bond_wei"] == str(BOND)
    assert pre["policy_version"] == 1
    assert pre["binding_required"] is True
    assert pre["pending_application_id"] == ""


def test_preflight_reports_a_pending_application(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["eligible"] is False
    assert pre["reason"] == "APPLICATION_PENDING"
    assert pre["pending_application_id"] == app_id


def test_preflight_reports_a_paused_gate(gate, direct_vm, direct_owner, direct_alice):
    direct_vm.sender = direct_owner
    gate.set_gate_paused(GATE_ID, True)
    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["reason"] == "GATE_PAUSED"


def test_preflight_on_an_unknown_gate(gate, direct_alice):
    pre = json.loads(gate.can_apply("nope", direct_alice))
    assert pre == {"eligible": False, "reason": "UNKNOWN_GATE"}


def test_binding_token_is_address_specific(gate, direct_alice, direct_bob):
    a = gate.binding_token(GATE_ID, direct_alice)
    b = gate.binding_token(GATE_ID, direct_bob)
    assert a != b
    assert a.startswith("glgate:" + GATE_ID + ":")
    assert hex_of(direct_alice) in a


def test_preflight_carries_the_binding_token(gate, direct_alice):
    pre = json.loads(gate.can_apply(GATE_ID, direct_alice))
    assert pre["binding_token"] == gate.binding_token(GATE_ID, direct_alice)


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://registry.example.com/x", True),
        ("https://sub.registry.example.com/x", True),
        ("https://boards.example.org/y", True),
        ("https://registry.example.com.evil.net/x", False),
        ("https://other.example.net/x", False),
        ("http://registry.example.com/x", False),
        ("not-a-url", False),
    ],
)
def test_host_checker_view(gate, url, allowed):
    assert gate.evidence_host_allowed(GATE_ID, url) is allowed


def test_host_checker_on_unknown_gate_is_false(gate):
    assert gate.evidence_host_allowed("nope", "https://registry.example.com/x") is False


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def test_applications_are_indexed_per_gate(gate, direct_vm, direct_alice, direct_bob):
    a = apply_as(gate, direct_vm, direct_alice)
    b = apply_as(gate, direct_vm, direct_bob)
    listing = json.loads(gate.list_applications(GATE_ID, 0, 10))
    assert listing["total"] == 2
    assert listing["application_ids"] == [a, b]


def test_applications_are_indexed_per_applicant(gate, direct_vm, direct_owner, direct_alice):
    from conftest import register_default_gate

    register_default_gate(gate, direct_vm, direct_owner, gate_id="other-gate")
    a = apply_as(gate, direct_vm, direct_alice)
    b = apply_as(gate, direct_vm, direct_alice, gate_id="other-gate")
    listing = json.loads(gate.get_applicant_applications(direct_alice, 0, 10))
    assert listing["total"] == 2
    assert listing["application_ids"] == [a, b]


def test_application_pagination(gate, direct_vm, direct_accounts, funded):
    ids = []
    for account in direct_accounts[:4]:
        direct_vm.deal(account, 10**19)
        ids.append(apply_as(gate, direct_vm, account))
    page = json.loads(gate.list_applications(GATE_ID, 1, 2))
    assert page["application_ids"] == ids[1:3]
    assert page["total"] == 4


def test_unknown_application_reads_empty(gate):
    assert gate.get_application("app_999") == ""


def test_gate_counters_track_intake(gate, direct_vm, direct_alice, direct_bob):
    apply_as(gate, direct_vm, direct_alice)
    apply_as(gate, direct_vm, direct_bob)
    stats = json.loads(gate.gate_stats(GATE_ID))
    assert stats["total_applications"] == 2
    assert stats["total_granted"] == 0
    assert stats["total_denied"] == 0
    assert stats["live_holders"] == 0


# ---------------------------------------------------------------------------
# Stale bond reclaim
# ---------------------------------------------------------------------------


def test_stale_bond_cannot_be_reclaimed_early(gate, direct_vm, direct_alice):
    """
    The reclaim window is deliberately longer than any plausible adjudication delay,
    so it cannot be used to withdraw an application that is about to be denied.
    """
    app_id = apply_as(gate, direct_vm, direct_alice)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("reclaimable from"):
        gate.claim_stale_application(app_id)


def test_stale_bond_is_reclaimable_after_the_window(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    locked_before = int(json.loads(gate.get_registry_stats())["locked_wei"])

    warp_time(direct_vm, "2026-02-01T12:00:00Z")  # past the seven day window
    direct_vm.sender = direct_alice
    refunded = gate.claim_stale_application(app_id)
    assert int(refunded) == BOND

    app = json.loads(gate.get_application(app_id))
    assert app["status"] == "STALE_REFUNDED"
    stats = json.loads(gate.get_registry_stats())
    assert int(stats["locked_wei"]) == locked_before - BOND
    assert stats["treasury_wei"] == "0", "a stale bond is refunded, never earned"


def test_only_the_applicant_can_reclaim(gate, direct_vm, direct_alice, direct_bob):
    app_id = apply_as(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-02-01T12:00:00Z")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the applicant"):
        gate.claim_stale_application(app_id)


def test_reclaim_frees_the_address_to_apply_again(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-02-01T12:00:00Z")
    direct_vm.sender = direct_alice
    gate.claim_stale_application(app_id)

    assert json.loads(gate.can_apply(GATE_ID, direct_alice))["eligible"] is True
    second = apply_as(gate, direct_vm, direct_alice)
    assert second != app_id


def test_reclaim_cannot_be_repeated(gate, direct_vm, direct_alice):
    app_id = apply_as(gate, direct_vm, direct_alice)
    warp_time(direct_vm, "2026-02-01T12:00:00Z")
    direct_vm.sender = direct_alice
    gate.claim_stale_application(app_id)
    with direct_vm.expect_revert("already"):
        gate.claim_stale_application(app_id)


def test_reclaim_of_an_unknown_application_is_rejected(gate, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown application"):
        gate.claim_stale_application("app_404")
