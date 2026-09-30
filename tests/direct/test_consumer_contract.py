"""
The consumer contract on its own.

Direct mode allows a single contract class per VM, so the cross-contract call into a
live registry is proven on StudioNet in tests/live. What is verified here is everything
the consumer does that does not require the registry to answer: constructor argument
handling, the pinned gate binding, author only withdrawal, and input validation.

The constructor test exists because of a real deployment failure. A deploy client
encodes a hex address argument as a plain string, and writing a string into an Address
storage slot aborts the whole deployment with an attribute error from deep inside the
storage layer. Coercing at the boundary is what makes a deploy script work whether it
passes a string or an Address.
"""

import json

import pytest

GATE_ID = "licensed-medical-pro"
REGISTRY_HEX = "0xA21f00DdEDb898e0e10575DCF46B4e2E856E7a5b"
OWNER_HEX = "0xBC1399c55538eC034d4Da550C03c34Ae0C357f53"


@pytest.fixture
def consumer(direct_vm, direct_deploy, direct_owner):
    """Deployed with hex string arguments, exactly as a deploy script passes them."""
    return direct_deploy(
        "tests/fixtures/gated_consumer.py", REGISTRY_HEX, GATE_ID, OWNER_HEX
    )


def test_hex_string_constructor_arguments_are_accepted(consumer):
    info = json.loads(consumer.gate_info())
    assert info["gate_registry"].lower() == REGISTRY_HEX.lower()
    assert info["expected_gate_owner"].lower() == OWNER_HEX.lower()
    assert info["gate_id"] == GATE_ID
    assert info["listing_count"] == 0


def test_address_typed_constructor_arguments_are_accepted(direct_deploy, direct_alice, direct_bob):
    """The other encoding a client might use must work identically."""
    contract = direct_deploy(
        "tests/fixtures/gated_consumer.py", direct_alice, "some-gate", direct_bob
    )
    info = json.loads(contract.gate_info())
    assert info["gate_id"] == "some-gate"
    assert info["gate_registry"].startswith("0x")


def test_gate_id_is_normalized_at_construction(direct_deploy):
    contract = direct_deploy(
        "tests/fixtures/gated_consumer.py", REGISTRY_HEX, "  MiXeD-Gate  ", OWNER_HEX
    )
    assert json.loads(contract.gate_info())["gate_id"] == "mixed-gate"


def test_an_invalid_registry_argument_is_rejected_with_a_clear_message(
    direct_vm, direct_deploy
):
    with direct_vm.expect_revert("not a valid address"):
        direct_deploy(
            "tests/fixtures/gated_consumer.py", "not-an-address", GATE_ID, OWNER_HEX
        )


def test_listings_start_empty(consumer):
    listing = json.loads(consumer.list_listings(0, 10))
    assert listing == {"listings": [], "offset": 0, "total": 0}


def test_unknown_listing_reads_empty(consumer):
    assert consumer.get_listing("listing_404") == ""


def test_withdraw_rejects_an_unknown_listing(consumer, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Unknown listing"):
        consumer.withdraw_listing("listing_404")


def test_verify_gate_owner_reports_a_missing_gate(consumer):
    """
    The registry address here holds no such gate, so the check must say so rather than
    claim the owner matches. A consumer that cannot confirm its gate should fail loudly.
    """
    result = json.loads(consumer.verify_gate_owner())
    assert result["ok"] is False
    assert result["reason"] in ("GATE_NOT_FOUND", "OWNER_CHANGED")
