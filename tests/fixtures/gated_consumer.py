# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Gated Consumer
==============

A real consumer contract that defers its access control to a Semantic Access Gate.

This exists to prove the composability claim of the primitive with running code
rather than prose. It is a professional listings board: anyone may read it, but
only an address that currently satisfies the gate's natural language policy may
publish. The board itself contains no access logic, no allowlist, and no admin
approval path. It asks the gate one question and trusts the answer.

Integration shape demonstrated here, which is the shape every consumer should copy:

  1. Pin the pair (registry address, gate_id) at construction. Gate registration is
     permissionless, so a gate_id alone is not an identity.
  2. Pin the gate owner you expect and expose verify_gate_owner() so anyone can
     confirm the gate you defer to is still controlled by who you think. A gate
     owner can rewrite the policy, so ownership is part of your trust assumption.
  3. Call is_approved on every gated write. It is a cached boolean, so this costs
     one cross-contract view call and never triggers consensus.

Because the gate re-stamps records on every policy change, a consumer that follows
this shape automatically inherits policy updates. Nothing here needs redeploying or
migrating when the policy text changes.
"""

import json
import typing
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


def _as_address(value: typing.Any) -> Address:
    """
    Normalize an address argument to an Address.

    Constructor arguments arrive as whatever the deploying client encoded. A hex
    string is a perfectly reasonable thing for a deploy script to pass, and it must
    not be written into an Address storage slot untouched: the slot writer asks the
    value for its bytes and a str has none, which fails the whole deployment with an
    attribute error rather than a usable message.
    """
    if isinstance(value, Address):
        return value
    try:
        return Address(value)
    except Exception:
        raise gl.vm.UserError("[EXPECTED] Value is not a valid address")


@allow_storage
@dataclass
class Listing:
    listing_id: str
    author: Address
    title: str
    body: str
    created_at: u64
    withdrawn: bool


class GatedConsumer(gl.Contract):
    """A listings board whose publish permission is a natural language policy."""

    gate_registry: Address
    gate_id: str
    expected_gate_owner: Address
    deployer: Address

    listings: TreeMap[str, Listing]
    listing_index: TreeMap[u32, str]
    listing_count: u32

    def __init__(
        self, gate_registry: Address, gate_id: str, expected_gate_owner: Address
    ) -> None:
        self.gate_registry = _as_address(gate_registry)
        self.gate_id = gate_id.strip().lower()
        self.expected_gate_owner = _as_address(expected_gate_owner)
        self.deployer = gl.message.sender_address
        self.listing_count = u32(0)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _now(self) -> u64:
        try:
            if (
                hasattr(gl, "message_raw")
                and isinstance(gl.message_raw, dict)
                and "datetime" in gl.message_raw
            ):
                txt = str(gl.message_raw["datetime"]).strip()
                if txt:
                    if txt.endswith("Z"):
                        txt = txt[:-1] + "+00:00"
                    dt = datetime.fromisoformat(txt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return u64(int(dt.timestamp()))
            return u64(int(datetime.now(timezone.utc).timestamp()))
        except Exception:
            return u64(0)

    def _gate_says_approved(self, subject: Address) -> bool:
        """The entire access check. One cross-contract view call, no consensus."""
        return bool(
            gl.get_contract_at(self.gate_registry)
            .view()
            .is_approved(self.gate_id, subject)
        )

    # ------------------------------------------------------------------
    # Gated writes
    # ------------------------------------------------------------------

    @gl.public.write
    def publish_listing(self, title: str, body: str) -> str:
        """Publish a listing. Permitted only while the gate approves the sender."""
        author = gl.message.sender_address
        if not self._gate_says_approved(author):
            raise gl.vm.UserError(
                "[EXPECTED] Address is not approved by gate " + self.gate_id
            )

        clean_title = " ".join(title.split())
        clean_body = " ".join(body.split())
        if not (3 <= len(clean_title) <= 160):
            raise gl.vm.UserError("[EXPECTED] title must be 3 to 160 characters")
        if not (10 <= len(clean_body) <= 2000):
            raise gl.vm.UserError("[EXPECTED] body must be 10 to 2000 characters")

        listing_id = "listing_" + str(int(self.listing_count))
        self.listings[listing_id] = Listing(
            listing_id=listing_id,
            author=author,
            title=clean_title,
            body=clean_body,
            created_at=self._now(),
            withdrawn=False,
        )
        self.listing_index[u32(int(self.listing_count))] = listing_id
        self.listing_count = u32(int(self.listing_count) + 1)
        return listing_id

    @gl.public.write
    def withdraw_listing(self, listing_id: str) -> None:
        """
        Withdraw a listing you published.

        Deliberately not gated on is_approved. A professional whose access lapsed can
        still clean up after themselves, and the gate is about who may publish, not
        who may retract.
        """
        listing = self.listings.get(listing_id)
        if listing is None:
            raise gl.vm.UserError("[EXPECTED] Unknown listing: " + listing_id)
        if gl.message.sender_address != listing.author:
            raise gl.vm.UserError("[EXPECTED] Only the author can withdraw a listing")
        if bool(listing.withdrawn):
            raise gl.vm.UserError("[EXPECTED] Listing is already withdrawn")
        listing.withdrawn = True
        self.listings[listing_id] = listing

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    @gl.public.view
    def can_publish(self, subject: Address) -> bool:
        """Front end pre-flight. Proxies straight through to the gate."""
        return self._gate_says_approved(_as_address(subject))

    @gl.public.view
    def verify_gate_owner(self) -> str:
        """
        Confirm the gate this board defers to is still owned by the expected address.

        A gate owner can rewrite the policy at will, so the owner is part of what a
        consumer trusts. Surfacing this lets any user or auditor check the assumption
        without reading the registry themselves.
        """
        raw = (
            gl.get_contract_at(self.gate_registry)
            .view()
            .get_gate(self.gate_id)
        )
        if not raw:
            return json.dumps(
                {
                    "ok": False,
                    "reason": "GATE_NOT_FOUND",
                    "gate_id": self.gate_id,
                    "expected_owner": self.expected_gate_owner.as_hex,
                },
                sort_keys=True,
            )
        gate = json.loads(raw)
        actual = str(gate.get("owner", ""))
        matches = actual.lower() == self.expected_gate_owner.as_hex.lower()
        return json.dumps(
            {
                "ok": matches,
                "reason": "OK" if matches else "OWNER_CHANGED",
                "gate_id": self.gate_id,
                "expected_owner": self.expected_gate_owner.as_hex,
                "actual_owner": actual,
                "policy_version": int(gate.get("policy_version", 0)),
                "policy_text": str(gate.get("policy_text", "")),
            },
            sort_keys=True,
        )

    @gl.public.view
    def gate_info(self) -> str:
        """The gate binding this board was deployed against."""
        return json.dumps(
            {
                "gate_registry": self.gate_registry.as_hex,
                "gate_id": self.gate_id,
                "expected_gate_owner": self.expected_gate_owner.as_hex,
                "deployer": self.deployer.as_hex,
                "listing_count": int(self.listing_count),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_listing(self, listing_id: str) -> str:
        listing = self.listings.get(listing_id)
        if listing is None:
            return ""
        return json.dumps(
            {
                "listing_id": str(listing.listing_id),
                "author": listing.author.as_hex,
                "title": str(listing.title),
                "body": str(listing.body),
                "created_at": int(listing.created_at),
                "withdrawn": bool(listing.withdrawn),
            },
            sort_keys=True,
        )

    @gl.public.view
    def list_listings(self, offset: int, limit: int) -> str:
        total = int(self.listing_count)
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        out = []
        index = start
        while index < total and len(out) < count:
            lid = self.listing_index.get(u32(index))
            if lid is not None:
                listing = self.listings.get(str(lid))
                if listing is not None:
                    out.append(
                        {
                            "listing_id": str(listing.listing_id),
                            "author": listing.author.as_hex,
                            "title": str(listing.title),
                            "withdrawn": bool(listing.withdrawn),
                        }
                    )
            index += 1
        return json.dumps(
            {"total": total, "offset": start, "listings": out}, sort_keys=True
        )
