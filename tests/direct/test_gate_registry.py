"""
Gate registration, configuration and ownership.

The registry is permissionless: anyone can register a gate. That makes input
validation part of the security model, because a malformed gate would be a gate whose
policy validators cannot adjudicate consistently.
"""

import json

import pytest

from conftest import (
    ALLOWED_HOSTS,
    CONDITIONS,
    DISQUALIFIERS,
    GATE_ID,
    GATE_TITLE,
    POLICY_TEXT,
    ZERO_ADDRESS,
    hex_of,
    register_default_gate,
)


def test_registration_stores_every_field(gate, direct_vm, direct_owner):
    stored = json.loads(gate.get_gate(GATE_ID))
    assert stored["gate_id"] == GATE_ID
    assert stored["owner"].lower() == hex_of(direct_owner)
    assert stored["title"] == GATE_TITLE
    assert stored["policy_text"] == POLICY_TEXT
    assert stored["policy_version"] == 1
    assert stored["fetch_mode"] == "raw"
    assert stored["binding_required"] is True
    assert stored["require_grounded_quotes"] is True
    assert stored["paused"] is False
    assert stored["total_applications"] == 0
    assert stored["treasury_wei"] == "0"


def test_conditions_are_canonicalized_by_id(gate):
    """
    Stored conditions are sorted and re-serialized. Validators embed these exact bytes
    in their prompts, so the stored order must not depend on how the owner typed them.
    """
    stored = json.loads(gate.get_gate(GATE_ID))
    ids = [c["id"] for c in stored["conditions"]]
    assert ids == sorted(ids)


def test_condition_text_whitespace_is_collapsed(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    registry.register_gate(
        "ws-gate", "Whitespace", POLICY_TEXT,
        json.dumps([{"id": "c1", "text": "  spaced   out    condition text here  "}]),
        json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
    )
    stored = json.loads(registry.get_gate("ws-gate"))
    assert stored["conditions"][0]["text"] == "spaced out condition text here"


def test_hosts_are_lowercased_sorted_and_deduplicated(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    registry.register_gate(
        "host-gate", "Hosts", POLICY_TEXT,
        json.dumps(CONDITIONS), json.dumps([]),
        json.dumps(["ZZZ.example.com", "aaa.example.com", "ZZZ.example.com", ".mid.example.com"]),
        "raw", False, False, 3600, 0, 0, 0,
    )
    stored = json.loads(registry.get_gate("host-gate"))
    assert stored["allowed_hosts"] == ["aaa.example.com", "mid.example.com", "zzz.example.com"]


def test_gate_id_is_lowercased(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    returned = registry.register_gate(
        "MiXeD-Case", "Mixed", POLICY_TEXT, json.dumps(CONDITIONS),
        json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
    )
    assert returned == "mixed-case"
    assert json.loads(registry.get_gate("mixed-case"))["gate_id"] == "mixed-case"


def test_duplicate_gate_id_is_rejected(gate, direct_vm, direct_owner):
    with direct_vm.expect_revert("already registered"):
        register_default_gate(gate, direct_vm, direct_owner)


def test_anyone_can_register_their_own_gate(registry, direct_vm, direct_bob):
    """Permissionless registration is the point: builders do not ask anyone's leave."""
    register_default_gate(registry, direct_vm, direct_bob, gate_id="bob-gate")
    stored = json.loads(registry.get_gate("bob-gate"))
    assert stored["owner"].lower() == hex_of(direct_bob)


@pytest.mark.parametrize("bad_id", ["ab", "x" * 49, "Has Space", "under_score!", "-lead", ""])
def test_malformed_gate_ids_are_rejected(registry, direct_vm, direct_owner, bad_id):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert():
        registry.register_gate(
            bad_id, "Test Gate", POLICY_TEXT, json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_gate_id_cannot_contain_the_key_separator(registry, direct_vm, direct_owner):
    """
    Access records are keyed by gate_id plus holder. A separator inside a gate_id
    would let one gate forge a key belonging to another, so the id charset excludes it.
    """
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert():
        registry.register_gate(
            "evil|gate", "Test Gate", POLICY_TEXT, json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_policy_text_below_the_floor_is_rejected(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("policy_text must be"):
        registry.register_gate(
            "short-policy", "Test Gate", "too short", json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_gate_with_no_conditions_is_rejected(registry, direct_vm, direct_owner):
    """A gate with nothing to satisfy could never grant, so it must not be created."""
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("cannot be empty"):
        registry.register_gate(
            "empty-gate", "Test Gate", POLICY_TEXT, json.dumps([]),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_too_many_conditions_is_rejected(registry, direct_vm, direct_owner):
    """
    The cap is a consensus safeguard: an unbounded condition list would grow the prompt
    until model output truncates, and a truncated response is a divergence risk.
    """
    direct_vm.sender = direct_owner
    many = [{"id": f"c{i}", "text": f"Condition number {i} with sufficient text"} for i in range(9)]
    with direct_vm.expect_revert("at most 8"):
        registry.register_gate(
            "many-gate", "Test Gate", POLICY_TEXT, json.dumps(many),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_duplicate_condition_ids_are_rejected(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    dupes = [
        {"id": "same", "text": "First condition with enough text"},
        {"id": "same", "text": "Second condition with enough text"},
    ]
    with direct_vm.expect_revert("Duplicate id"):
        registry.register_gate(
            "dupe-gate", "Test Gate", POLICY_TEXT, json.dumps(dupes),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_condition_text_too_short_is_rejected(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Text for c1 must be"):
        registry.register_gate(
            "tiny-cond", "Test Gate", POLICY_TEXT,
            json.dumps([{"id": "c1", "text": "short"}]),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_invalid_conditions_json_is_rejected(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("valid JSON"):
        registry.register_gate(
            "bad-json", "Test Gate", POLICY_TEXT, "{not json",
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_conditions_must_be_an_array(registry, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("JSON array"):
        registry.register_gate(
            "obj-json", "Test Gate", POLICY_TEXT, json.dumps({"id": "c1"}),
            json.dumps([]), json.dumps([]), "raw", False, False, 3600, 0, 0, 0,
        )


@pytest.mark.parametrize("bad_host", ["not a host", "no-tld", "http://x.com", "spaces .com"])
def test_malformed_hosts_are_rejected(registry, direct_vm, direct_owner, bad_host):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Invalid host"):
        registry.register_gate(
            "hostfail", "Test Gate", POLICY_TEXT, json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([bad_host]), "raw", False, False, 3600, 0, 0, 0,
        )


def test_invalid_fetch_mode_is_rejected(registry, direct_vm, direct_owner):
    """
    Fetch mode is fixed per gate. Allowing a runtime fallback would let one node read
    rendered text while another read raw HTML, which is a divergence waiting to happen.
    """
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("fetch_mode"):
        registry.register_gate(
            "mode-gate", "Test Gate", POLICY_TEXT, json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([]), "screenshot", False, False, 3600, 0, 0, 0,
        )


@pytest.mark.parametrize("ttl", [0, 59, 315360001])
def test_ttl_outside_the_permitted_band_is_rejected(registry, direct_vm, direct_owner, ttl):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("access_ttl_seconds"):
        registry.register_gate(
            f"ttl-{ttl}", "Test Gate", POLICY_TEXT, json.dumps(CONDITIONS),
            json.dumps([]), json.dumps([]), "raw", False, False, ttl, 0, 0, 0,
        )


def test_render_mode_gate_is_accepted(registry, direct_vm, direct_owner):
    register_default_gate(registry, direct_vm, direct_owner, gate_id="render-gate", fetch_mode="render")
    assert json.loads(registry.get_gate("render-gate"))["fetch_mode"] == "render"


def test_registry_indexes_and_paginates_gates(registry, direct_vm, direct_owner):
    for i in range(5):
        register_default_gate(registry, direct_vm, direct_owner, gate_id=f"gate-{i}")
    page = json.loads(registry.list_gates(0, 3))
    assert page["total"] == 5
    assert page["gate_ids"] == ["gate-0", "gate-1", "gate-2"]
    rest = json.loads(registry.list_gates(3, 10))
    assert rest["gate_ids"] == ["gate-3", "gate-4"]


def test_unknown_gate_reads_return_empty_not_an_error(registry):
    """Views degrade to empty so a consumer front end never has to catch a revert."""
    assert registry.get_gate("nope") == ""
    assert registry.get_policy("nope") == ""
    assert registry.gate_stats("nope") == ""
    assert registry.list_holders("nope", 0, 10) == ""


# ---------------------------------------------------------------------------
# Configuration and ownership
# ---------------------------------------------------------------------------


def test_only_the_owner_can_update_configuration(gate, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        gate.update_gate_config(
            GATE_ID, json.dumps(ALLOWED_HOSTS), "raw", True, True, 7200, 0, 0, 0
        )


def test_config_update_leaves_the_policy_version_alone(gate, direct_vm, direct_owner):
    """
    Mechanical settings are not the policy. Bumping the version here would invalidate
    every live grant for a change that did not alter what the policy requires.
    """
    before = json.loads(gate.get_gate(GATE_ID))["policy_version"]
    direct_vm.sender = direct_owner
    gate.update_gate_config(
        GATE_ID, json.dumps(["registry.example.com"]), "render", False, False, 7200, 5, 6, 60
    )
    after = json.loads(gate.get_gate(GATE_ID))
    assert after["policy_version"] == before
    assert after["fetch_mode"] == "render"
    assert after["binding_required"] is False
    assert after["require_grounded_quotes"] is False
    assert after["access_ttl_seconds"] == 7200
    assert after["bond_wei"] == "5"
    assert after["challenge_stake_wei"] == "6"
    assert after["reapply_cooldown_seconds"] == 60


def test_only_the_owner_can_pause(gate, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        gate.set_gate_paused(GATE_ID, True)


def test_pause_and_resume_round_trip(gate, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    gate.set_gate_paused(GATE_ID, True)
    assert json.loads(gate.get_gate(GATE_ID))["paused"] is True
    gate.set_gate_paused(GATE_ID, False)
    assert json.loads(gate.get_gate(GATE_ID))["paused"] is False


def test_ownership_transfer_moves_control(gate, direct_vm, direct_owner, direct_bob):
    direct_vm.sender = direct_owner
    gate.transfer_gate_ownership(GATE_ID, direct_bob)
    assert json.loads(gate.get_gate(GATE_ID))["owner"].lower() == hex_of(direct_bob)

    # The former owner has no authority left.
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Only the gate owner"):
        gate.set_gate_paused(GATE_ID, True)

    # The new owner does.
    direct_vm.sender = direct_bob
    gate.set_gate_paused(GATE_ID, True)
    assert json.loads(gate.get_gate(GATE_ID))["paused"] is True


def test_ownership_cannot_be_transferred_to_the_zero_address(gate, direct_vm, direct_owner):
    """Burning a gate would leave its policy frozen and its treasury unreachable."""
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("zero address"):
        gate.transfer_gate_ownership(GATE_ID, ZERO_ADDRESS)


def test_treasury_withdrawal_rejects_an_empty_treasury(gate, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("exceeds gate treasury"):
        gate.withdraw_treasury(GATE_ID, 1)


def test_treasury_withdrawal_rejects_a_non_owner(gate, direct_vm, direct_bob):
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the gate owner"):
        gate.withdraw_treasury(GATE_ID, 1)


def test_treasury_withdrawal_rejects_a_non_positive_amount(gate, direct_vm, direct_owner):
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("must be positive"):
        gate.withdraw_treasury(GATE_ID, 0)


def test_policy_view_exposes_exactly_what_validators_adjudicate(gate):
    policy = json.loads(gate.get_policy(GATE_ID))
    assert policy["policy_text"] == POLICY_TEXT
    assert [c["id"] for c in policy["conditions"]] == sorted(c["id"] for c in CONDITIONS)
    assert [d["id"] for d in policy["disqualifiers"]] == [d["id"] for d in DISQUALIFIERS]
    assert policy["policy_version"] == 1
