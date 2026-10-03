"""
Live StudioNet: real consensus adjudication and real cross contract composability.

Every adjudication in this file is genuine. Validators on the Studio network fetch the
evidence document over HTTPS themselves, prompt their own language models, reduce their
own findings, and the transaction commits only when they agree with the leader. Nothing
is stubbed and no result is fabricated.

The order of the tests matters, because they build one live story: an address applies
with real evidence, is granted access by consensus, is admitted by an independent
consumer contract that knows nothing about the evidence, loses that admission the moment
the gate owner changes the policy, and is admitted again once the policy is restored and
the evidence is re-adjudicated.
"""

import json

import pytest

from conftest import (
    BOUND_GATE_CONDITIONS,
    CONSUMER_GATE_CONDITIONS,
    CONSUMER_GATE_DISQUALIFIERS,
    CONSUMER_GATE_POLICY,
    EVIDENCE_HOST_ECHO,
    EVIDENCE_HOST_REAL,
    LIVE_BOND,
    LIVE_STAKE,
    LIVE_TTL,
    README_URL,
    apply_and_get_id,
    echo_url,
    ensure_gate,
    expect_revert,
    fetch_text,
    licence_document,
    read,
    read_json,
    release_if_held,
    send,
    unique_gate_id,
)

pytestmark = pytest.mark.live


def adjudicate_fresh(client, registry, gate_id, urls, note, bond):
    """
    Take one address through a complete live round and return the settled application.

    Any access already held is released first so the round is a real adjudication rather
    than a rejection for already holding access, which is what makes this suite
    repeatable against a deployment that has been used before.
    """
    release_if_held(client, registry, gate_id, client.local_account.address)
    app_id = apply_and_get_id(client, registry, gate_id, urls, note, bond)
    send(client, registry, "adjudicate", [app_id])
    return read_json(client, registry, "get_application", [app_id])


def restore_frame(client, registry, gate_id, hosts, binding_required, grounded, bond, stake):
    """Put a gate's adjudication frame back, so the deployment is left reusable."""
    send(
        client, registry, "update_gate_config",
        [gate_id, json.dumps(hosts), "raw", binding_required, grounded,
         LIVE_TTL, bond, stake, 0],
    )


# ---------------------------------------------------------------------------
# The evidence itself
# ---------------------------------------------------------------------------


def test_the_real_evidence_document_is_publicly_retrievable(owner):
    """
    Confirm from the test process what the validators will independently fetch. If this
    fails the network is at fault, not the contract, and the distinction should be
    visible rather than buried in a consensus error.
    """
    body = fetch_text(README_URL)
    assert len(body) > 200
    assert "execute Python code" in body
    assert "validator consensus" in body


# ---------------------------------------------------------------------------
# A real grant
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_grant(owner, registry_address, consumer_gate):
    """One real adjudication, shared by the assertions that inspect its result."""
    return adjudicate_fresh(
        owner,
        registry_address,
        consumer_gate,
        [README_URL],
        "Public repository documenting a consensus based contract platform.",
        LIVE_BOND,
    )


def test_consensus_grants_access_on_real_evidence(live_grant):
    """
    The primitive doing its job. Validators read a document nobody wrote for this test
    and independently agreed that it satisfies a policy written in plain English.
    """
    assert live_grant["status"] == "GRANTED", (
        f"expected a grant, got {live_grant['status']} "
        f"({live_grant['denial_code']}): {live_grant['reasoning']}"
    )
    assert live_grant["decision"] == "GRANTED"
    assert live_grant["denial_code"] == ""
    assert live_grant["evidence_ok"] is True
    assert live_grant["failed_ids"] == []


def test_the_grant_is_justified_by_quotes_from_the_document(live_grant):
    """
    Every satisfied condition carries a span the contract found verbatim in the bytes it
    fetched. This is the anti hallucination check, verified on chain rather than trusted.
    """
    body = fetch_text(README_URL)
    ids = sorted(c["id"] for c in live_grant["conditions"])
    assert ids == sorted(c["id"] for c in CONSUMER_GATE_CONDITIONS)

    for condition in live_grant["conditions"]:
        assert condition["verdict"] == "SATISFIED", condition
        assert condition["grounded"] is True, condition
        assert condition["quote"], condition
        normalized_quote = " ".join(condition["quote"].split()).lower()
        normalized_body = " ".join(body.split()).lower()
        assert normalized_quote in normalized_body, (
            f"quote for {condition['id']} is not present in the fetched document"
        )


def test_no_disqualifier_was_found(live_grant):
    ids = [d["id"] for d in live_grant["disqualifiers"]]
    assert ids == [d["id"] for d in CONSUMER_GATE_DISQUALIFIERS]
    assert all(d["present"] is False for d in live_grant["disqualifiers"])


def test_the_fetch_is_recorded_in_the_audit_trail(live_grant):
    status = live_grant["evidence_status"]
    assert len(status) == 1
    assert status[0]["url"] == README_URL
    assert status[0]["status"] == 200
    assert status[0]["retrievable"] is True


def test_the_grant_produced_a_live_version_stamped_record(
    owner, owner_account, registry_address, consumer_gate, live_grant
):
    assert read(owner, registry_address, "is_approved", [consumer_gate, owner_account.address]) is True

    record = read_json(owner, registry_address, "get_access_record", [consumer_gate, owner_account.address])
    assert record["status"] == "ACTIVE"
    assert record["application_id"] == live_grant["application_id"]
    assert int(record["expires_at"]) - int(record["granted_at"]) == LIVE_TTL

    gate = read_json(owner, registry_address, "get_gate", [consumer_gate])
    assert int(record["policy_version"]) == int(gate["policy_version"])

    status = read_json(owner, registry_address, "access_status", [consumer_gate, owner_account.address])
    assert status["approved"] is True
    assert status["reason"] == "OK"
    assert status["seconds_remaining"] > 0


def test_the_bond_is_held_as_collateral_not_taken(owner, registry_address, consumer_gate, live_grant):
    record = read_json(
        owner, registry_address, "get_access_record",
        [consumer_gate, owner.local_account.address],
    )
    assert record["deposit_wei"] == str(LIVE_BOND)


def test_a_granted_address_cannot_reapply_while_live(
    owner, owner_account, registry_address, consumer_gate, live_grant
):
    pre = read_json(owner, registry_address, "can_apply", [consumer_gate, owner_account.address])
    assert pre["eligible"] is False
    assert pre["reason"] == "ALREADY_APPROVED"

    failure = expect_revert(
        owner, registry_address, "apply_for_access",
        [consumer_gate, json.dumps([README_URL]), "again"], value=LIVE_BOND,
    )
    assert "already holds live access" in failure


def test_the_holder_appears_in_the_gate_listing(owner, owner_account, registry_address, consumer_gate, live_grant):
    holders = read_json(owner, registry_address, "list_holders", [consumer_gate, 0, 100])
    mine = [h for h in holders["holders"] if h["holder"].lower() == owner_account.address.lower()]
    assert len(mine) == 1
    assert mine[0]["live"] is True

    stats = read_json(owner, registry_address, "gate_stats", [consumer_gate])
    assert int(stats["live_holders"]) >= 1
    assert int(stats["total_granted"]) >= 1


# ---------------------------------------------------------------------------
# Cross contract composability
# ---------------------------------------------------------------------------


def test_the_consumer_contract_admits_the_approved_address(
    owner, owner_account, consumer_address, live_grant
):
    """
    The composability claim, proven across two separately deployed contracts. The
    consumer holds no allowlist and no admin approval path; it asks the gate and gets a
    cached boolean.
    """
    assert read(owner, consumer_address, "can_publish", [owner_account.address]) is True


def test_the_consumer_confirms_the_gate_owner_it_pinned(owner, owner_account, consumer_address, live_grant):
    result = read_json(owner, consumer_address, "verify_gate_owner", [])
    assert result["ok"] is True, result
    assert result["reason"] == "OK"
    assert result["actual_owner"].lower() == owner_account.address.lower()
    assert result["policy_text"] == CONSUMER_GATE_POLICY


def test_an_unapproved_address_is_refused_by_the_consumer(other, consumer_address, live_grant):
    """The other identity never applied, so the gate answers no and the board obeys."""
    assert read(other, consumer_address, "can_publish", [other.local_account.address]) is False

    failure = expect_revert(
        other, consumer_address, "publish_listing",
        ["Unapproved listing attempt", "This address never established the gate policy."],
    )
    assert "not approved by gate" in failure


def test_an_approved_address_can_write_to_the_consumer(owner, owner_account, consumer_address, live_grant):
    before = json.loads(read(owner, consumer_address, "list_listings", [0, 100]))["total"]
    send(
        owner, consumer_address, "publish_listing",
        [
            "Consensus verified operator listing",
            "Published by an address admitted through a natural language access policy "
            "adjudicated by GenLayer validators, with no allowlist and no admin approval.",
        ],
    )
    after = json.loads(read(owner, consumer_address, "list_listings", [0, 100]))
    assert after["total"] == before + 1

    published = after["listings"][-1]
    assert published["author"].lower() == owner_account.address.lower()
    assert published["withdrawn"] is False


# ---------------------------------------------------------------------------
# A real denial
# ---------------------------------------------------------------------------


def test_consensus_denies_evidence_that_does_not_satisfy_the_policy(owner, registry_address):
    """
    The same document, a different policy. Validators independently agree the evidence
    does not establish a medical licence, and the denial names the condition that failed.
    """
    gate_id = "live-mismatch-gate"
    ensure_gate(
        owner, registry_address, gate_id,
        title="Licensed Physicians Only",
        policy_text=(
            "Access is limited to individuals whose evidence document is an official "
            "medical licensing record naming them as a physician holding a current "
            "licence to practise medicine, issued by a government medical board."
        ),
        conditions=[
            {
                "id": "c1_medical_licence",
                "text": "The document is an official medical licensing record naming a licensed physician",
            }
        ],
        allowed_hosts=["raw.githubusercontent.com"],
        binding_required=False,
        bond_wei=LIVE_BOND,
    )

    treasury_before = int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"])

    settled = adjudicate_fresh(
        owner, registry_address, gate_id, [README_URL],
        "Submitting a software repository against a medical licence policy.", LIVE_BOND,
    )
    assert settled["status"] == "DENIED", settled["reasoning"]
    assert settled["denial_code"] == "CONDITIONS_NOT_SATISFIED"
    assert settled["failed_ids"] == ["c1_medical_licence"]
    assert settled["evidence_ok"] is True, "the document was fetched; it simply did not qualify"
    assert settled["evidence_status"][0]["status"] == 200
    assert read(owner, registry_address, "is_approved", [gate_id, owner.local_account.address]) is False

    treasury_after = int(read_json(owner, registry_address, "get_gate", [gate_id])["treasury_wei"])
    assert treasury_after == treasury_before + LIVE_BOND, "a denied bond is forfeited to the gate"


# ---------------------------------------------------------------------------
# Wallet binding
# ---------------------------------------------------------------------------


def test_evidence_without_the_binding_token_is_denied(owner, registry_address, bound_gate):
    """
    Proof of wallet control, checked in code against the bytes fetched. The document
    describes a perfectly valid licence and belongs to nobody in particular, so it is
    refused before the model is ever consulted.
    """
    document = licence_document("no wallet token appears on this page")
    settled = adjudicate_fresh(
        owner, registry_address, bound_gate, [echo_url(document)],
        "Licence record without a wallet proof.", LIVE_BOND,
    )
    assert settled["status"] == "DENIED", settled["reasoning"]
    assert settled["denial_code"] == "BINDING_TOKEN_MISSING"
    assert settled["binding_ok"] is False
    assert settled["conditions"] == [], "no model call is made once binding fails"


def test_another_addresses_binding_token_does_not_transfer(
    owner, other, registry_address, bound_gate
):
    """
    The impersonation case, live. A document carrying the owner's token is submitted by
    the other identity from its own address, and is refused. The token names the address
    it authorises, so republishing someone else's proof establishes nothing.
    """
    owners_token = read(
        owner, registry_address, "binding_token", [bound_gate, owner.local_account.address]
    )
    document = licence_document(owners_token)

    settled = adjudicate_fresh(
        other, registry_address, bound_gate, [echo_url(document)],
        "Submitting a document that carries a different wallet's token.", LIVE_BOND,
    )
    assert settled["status"] == "DENIED", settled["reasoning"]
    assert settled["denial_code"] == "BINDING_TOKEN_MISSING"
    assert read(owner, registry_address, "is_approved", [bound_gate, other.local_account.address]) is False


def test_correctly_bound_evidence_is_granted(owner, owner_account, registry_address, bound_gate):
    """
    The complete wallet bound path. The applicant publishes the gate's token for their
    own address on the evidence document, validators fetch it, the contract confirms the
    token in the fetched bytes, and consensus grants access.
    """
    token = read(owner, registry_address, "binding_token", [bound_gate, owner_account.address])
    document = licence_document(token)
    url = echo_url(document)

    served = fetch_text(url)
    assert token in served, "the evidence host is not returning the token as published"

    settled = adjudicate_fresh(
        owner, registry_address, bound_gate, [url],
        "Licence record carrying this wallet's binding token.", LIVE_BOND,
    )
    assert settled["status"] == "GRANTED", (
        f"expected a grant, got {settled['denial_code']}: {settled['reasoning']}"
    )
    assert settled["binding_ok"] is True
    assert [c["id"] for c in settled["conditions"]] == [
        c["id"] for c in BOUND_GATE_CONDITIONS
    ]
    assert all(c["grounded"] for c in settled["conditions"])
    assert read(owner, registry_address, "is_approved", [bound_gate, owner_account.address]) is True


# ---------------------------------------------------------------------------
# Policy invalidation, and restoring the deployment
# ---------------------------------------------------------------------------


def test_a_policy_change_invalidates_the_grant_and_closes_the_consumer(
    owner, owner_account, registry_address, consumer_address, consumer_gate, live_grant
):
    """
    The headline guarantee, end to end on chain. The gate owner adds one condition the
    evidence cannot satisfy. No holder record is touched and no consumer is redeployed,
    yet the approved address loses admission to the consumer contract immediately.

    The original policy is restored at the end of this test and the access re-adjudicated,
    so the live deployment is left in a working state.
    """
    gate_before = read_json(owner, registry_address, "get_gate", [consumer_gate])
    version_before = int(gate_before["policy_version"])
    assert read(owner, consumer_address, "can_publish", [owner_account.address]) is True

    tightened = CONSUMER_GATE_CONDITIONS + [
        {
            "id": "c3_iso_certified",
            "text": "The document states that the software holds ISO 27001 certification",
        }
    ]
    send(
        owner, registry_address, "update_policy",
        [
            consumer_gate,
            CONSUMER_GATE_POLICY + " Applicants must additionally evidence ISO 27001 certification.",
            json.dumps(tightened),
            json.dumps(CONSUMER_GATE_DISQUALIFIERS),
        ],
    )

    gate_after = read_json(owner, registry_address, "get_gate", [consumer_gate])
    assert int(gate_after["policy_version"]) == version_before + 1

    # The record is untouched, and no longer counts.
    record = read_json(owner, registry_address, "get_access_record", [consumer_gate, owner_account.address])
    assert record["status"] == "ACTIVE", "invalidation rewrites nothing"
    assert int(record["policy_version"]) == version_before

    assert read(owner, registry_address, "is_approved", [consumer_gate, owner_account.address]) is False
    status = read_json(owner, registry_address, "access_status", [consumer_gate, owner_account.address])
    assert status["approved"] is False
    assert status["reason"] == "POLICY_SUPERSEDED"
    assert status["record_policy_version"] == version_before
    assert status["current_policy_version"] == version_before + 1

    # The consumer inherits the change with no deployment and no migration.
    assert read(owner, consumer_address, "can_publish", [owner_account.address]) is False
    failure = expect_revert(
        owner, consumer_address, "publish_listing",
        ["Listing after policy change", "This should be refused because the gate policy moved on."],
    )
    assert "not approved by gate" in failure

    # Re-adjudicating the same evidence under the tightened policy still fails, which is
    # what makes invalidation meaningful rather than cosmetic.
    settled = adjudicate_fresh(
        owner, registry_address, consumer_gate, [README_URL],
        "Same evidence, tightened policy.", LIVE_BOND,
    )
    assert settled["status"] == "DENIED", settled["reasoning"]
    assert "c3_iso_certified" in settled["failed_ids"]

    # Restore the gate so the deployment is left usable, and confirm access returns.
    send(
        owner, registry_address, "update_policy",
        [
            consumer_gate,
            CONSUMER_GATE_POLICY,
            json.dumps(CONSUMER_GATE_CONDITIONS),
            json.dumps(CONSUMER_GATE_DISQUALIFIERS),
        ],
    )
    restored = read_json(owner, registry_address, "get_policy", [consumer_gate])
    assert restored["policy_text"] == CONSUMER_GATE_POLICY
    assert int(restored["policy_version"]) == version_before + 2

    regrant = adjudicate_fresh(
        owner, registry_address, consumer_gate, [README_URL],
        "Re-established under the restored policy.", LIVE_BOND,
    )
    assert regrant["status"] == "GRANTED", regrant["reasoning"]
    assert read(owner, registry_address, "is_approved", [consumer_gate, owner_account.address]) is True
    assert read(owner, consumer_address, "can_publish", [owner_account.address]) is True


def test_the_registry_accounting_is_consistent_after_the_live_run(owner, registry_address):
    """A final cross check: the registry total equals the sum of the gate treasuries."""
    stats = read_json(owner, registry_address, "get_registry_stats", [])
    listing = read_json(owner, registry_address, "list_gates", [0, 200])
    summed = sum(
        int(read_json(owner, registry_address, "get_gate", [g])["treasury_wei"])
        for g in listing["gate_ids"]
    )
    assert int(stats["treasury_wei"]) == summed
    assert int(stats["locked_wei"]) >= 0


def test_a_renewal_refunds_the_deposit_behind_the_record_it_replaces(
    owner, registry_address
):
    """
    Live proof that a renewal cannot strand a holder's collateral.

    An access record lives at one slot per holder, so re-granting has to replace
    whatever is in it. The record being replaced here was invalidated by a rules change
    rather than deleted, so it is still ACTIVE and still holding the deposit that backed
    it. Overwriting it would leave that deposit inside the registry's locked total with
    no record pointing at it and no method able to reach it.

    Two real adjudications run here. The assertion that matters is the last one: after
    the renewal the holder releases their one live record and the registry's locked total
    returns to exactly what it was before any of this started, which is only true if
    nothing was stranded on the way.
    """
    gate_id = unique_gate_id("live-renewal")
    ensure_gate(
        owner, registry_address, gate_id,
        title="Renewal Settlement Gate",
        policy_text=CONSUMER_GATE_POLICY,
        conditions=CONSUMER_GATE_CONDITIONS,
        disqualifiers=CONSUMER_GATE_DISQUALIFIERS,
        allowed_hosts=[EVIDENCE_HOST_REAL],
        binding_required=False,
        require_grounded_quotes=True,
        bond_wei=LIVE_BOND,
    )
    address = owner.local_account.address
    baseline = int(read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"])

    first = adjudicate_fresh(
        owner, registry_address, gate_id, [README_URL], "Original grant.", LIVE_BOND
    )
    assert first["status"] == "GRANTED", first["reasoning"]
    assert read(owner, registry_address, "is_approved", [gate_id, address]) is True
    assert int(
        read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]
    ) == baseline + LIVE_BOND

    # Invalidate the grant without touching the record, by moving the rules version.
    # Nothing is written to the record: it stays ACTIVE and keeps its deposit.
    send(
        owner, registry_address, "update_gate_config",
        [gate_id, json.dumps([EVIDENCE_HOST_REAL]), "raw", False,
         False,  # quote grounding switched off: an eligibility change
         LIVE_TTL, LIVE_BOND, LIVE_STAKE, 0],
    )
    assert read(owner, registry_address, "is_approved", [gate_id, address]) is False
    stale = read_json(owner, registry_address, "get_access_record", [gate_id, address])
    assert stale["status"] == "ACTIVE", "invalidation rewrites nothing"
    assert stale["deposit_wei"] == str(LIVE_BOND), "the lapsed record still holds its deposit"
    assert read_json(owner, registry_address, "access_status",
                     [gate_id, address])["reason"] == "RULES_SUPERSEDED"

    # Re-qualify under the new frame WITHOUT releasing first. This is the renewal.
    app_id = apply_and_get_id(
        owner, registry_address, gate_id, [README_URL], "Renewal under the new frame.", LIVE_BOND
    )
    send(owner, registry_address, "adjudicate", [app_id])
    renewed = read_json(owner, registry_address, "get_application", [app_id])
    assert renewed["status"] == "GRANTED", renewed["reasoning"]
    assert read(owner, registry_address, "is_approved", [gate_id, address]) is True

    # The replaced record was archived with its deposit settled, not overwritten.
    history = read_json(owner, registry_address, "get_access_history", [gate_id, address, 0, 10])
    assert int(history["total"]) >= 1
    archived = history["records"][-1]
    assert archived["application_id"] == first["application_id"]
    assert archived["status"] == "RENEWED"
    assert archived["deposit_wei"] == "0", "the replaced deposit was paid out"
    assert app_id in archived["close_reason"]

    # Exactly one bond is behind the live record, and the registry's obligation never
    # grew to two.
    live = read_json(owner, registry_address, "get_access_record", [gate_id, address])
    assert live["application_id"] == app_id
    assert live["deposit_wei"] == str(LIVE_BOND)
    assert int(
        read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]
    ) == baseline + LIVE_BOND

    # The decisive check: the holder can still get every wei back.
    send(owner, registry_address, "release_access", [gate_id])
    assert int(
        read_json(owner, registry_address, "get_registry_stats", [])["locked_wei"]
    ) == baseline, "the renewal stranded funds that can never be reclaimed"

    restore_frame(
        owner, registry_address, gate_id, [EVIDENCE_HOST_REAL], False, True, LIVE_BOND, LIVE_STAKE
    )
