# Integrating the Semantic Access Gate

How to defer your contract's access control to a natural language policy.

StudioNet registry: `0xA21f00DdEDb898e0e10575DCF46B4e2E856E7a5b`

## The one call

```python
gl.get_contract_at(REGISTRY).view().is_approved(GATE_ID, address)
```

It returns a boolean. It reads a cached record and triggers no consensus, so it is
cheap enough to put in front of every gated method.

## A consumer contract

The full worked example is `tests/fixtures/gated_consumer.py`, deployed alongside the
registry at `0xE31855910183e00b69906435EA956CAd0679F61C`. The pattern is:

```python
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *


class MyGatedContract(gl.Contract):
    gate_registry: Address
    gate_id: str
    expected_gate_owner: Address

    def __init__(
        self, gate_registry: Address, gate_id: str, expected_gate_owner: Address
    ) -> None:
        # Pin the pair at construction. A gate_id on its own is not an identity,
        # because anyone can register a gate under any unused id.
        self.gate_registry = gate_registry
        self.gate_id = gate_id.strip().lower()
        self.expected_gate_owner = expected_gate_owner

    def _approved(self, subject: Address) -> bool:
        return bool(
            gl.get_contract_at(self.gate_registry)
            .view()
            .is_approved(self.gate_id, subject)
        )

    @gl.public.write
    def do_the_gated_thing(self, payload: str) -> None:
        if not self._approved(gl.message.sender_address):
            raise gl.vm.UserError(
                "[EXPECTED] Address is not approved by gate " + self.gate_id
            )
        # ... your logic

    @gl.public.view
    def can_act(self, subject: Address) -> bool:
        """Expose the check so a front end can disable a button instead of failing."""
        return self._approved(subject)
```

Three things that example gets right and that are easy to get wrong.

**Pin the registry address and the gate id at construction.** Registration is
permissionless. If you resolve a gate by id at call time from user input, a caller can
point you at a gate they own.

**Pin and expose the gate owner.** A gate owner can rewrite the policy, so who owns the
gate is part of what your contract trusts. The example exposes `verify_gate_owner`,
which reads `get_gate` and compares the stored owner against the pinned expectation, so
any user or auditor can check the assumption without reading the registry themselves.

**Do not gate the exit.** In the example, publishing requires approval and withdrawing a
listing does not. A professional whose access lapsed should still be able to clean up
after themselves. Decide deliberately which of your methods are about who may act and
which are about who may undo.

If you need coarse behaviour without a cross-contract call on every write, cache the
boolean with the policy version you saw and re-check when the version moves. Read
`get_policy` for the current version.

## Registering a gate

```python
import json
import os
from genlayer_py import create_account, create_client
from genlayer_py.chains import studionet

client = create_client(
    chain=studionet,
    account=create_account(os.environ["GENLAYER_DEPLOYER_KEY"]),
)

conditions = [
    {
        "id": "c1_active_licence",
        "text": "The evidence shows a currently active licence to practise medicine",
    },
    {
        "id": "c2_board_certified",
        "text": "The evidence shows board certification in a recognised clinical specialty",
    },
]

disqualifiers = [
    {
        "id": "d1_disciplinary",
        "text": "The evidence shows a disciplinary action, suspension or sanction",
    }
]

tx = client.write_contract(
    address=REGISTRY,
    function_name="register_gate",
    args=[
        "licensed-physicians",                      # gate_id
        "Licensed Medical Professionals",           # title
        "Access is limited to individuals who hold a currently active licence to "
        "practise medicine, who are board certified in a recognised clinical "
        "specialty, and who have no disciplinary action recorded against that "
        "licence.",                                 # policy_text
        json.dumps(conditions),
        json.dumps(disqualifiers),
        json.dumps(["medicalboard.example.gov", "abim.org"]),  # allowed_hosts
        "raw",                                      # fetch_mode
        True,                                       # binding_required
        True,                                       # require_grounded_quotes
        7776000,                                    # access_ttl_seconds, 90 days
        10**16,                                     # bond_wei, 0.01 GEN
        5 * 10**16,                                 # challenge_stake_wei
        86400,                                      # reapply_cooldown_seconds
    ],
)
client.wait_for_transaction_receipt(tx, retries=60, interval=3000)
```

Check the receipt properly. A StudioNet transaction can report ACCEPTED while the call
inside it raised, and the top level status will not tell you. See
`tests/live/conftest.py::execution_failure` for a checker that reads the leader receipts
correctly, including the case where consensus rotated leaders mid transaction.

### Limits

| Field | Constraint |
|---|---|
| `gate_id` | 3 to 48 characters, `[a-z0-9]` then `[a-z0-9._-]`, unique in the registry |
| `title` | 3 to 120 characters |
| `policy_text` | 40 to 4000 characters |
| `conditions_json` | 1 to 8 entries, each `{"id", "text"}`, text 12 to 400 characters |
| `disqualifiers_json` | 0 to 4 entries, same shape |
| `allowed_hosts_json` | 0 to 12 hostnames, empty means any host |
| `fetch_mode` | `"raw"` or `"render"` |
| `access_ttl_seconds` | 60 to 315360000 |
| `reapply_cooldown_seconds` | 0 to 31536000 |

Condition ids are your handle on the outcome. A denial returns `failed_ids`, so give them
names you will want to read in a support ticket.

## Writing a policy that adjudicates consistently

The policy text is context. The conditions are what gets decided. Validators are asked
about each condition separately and the contract combines the answers, so a condition
that bundles several requirements is a condition that different nodes can answer
differently.

**Split conjunctions into separate conditions.** "Holds an active licence and has no
sanctions" is two facts. A validator that finds the licence but is unsure about
sanctions has no consistent way to answer one question about both. Split it, and put the
sanction half in `disqualifiers`.

**Say what the document must show, not what the applicant must be.** "Is a licensed
physician" invites a judgement about a person. "The document shows a currently active
licence to practise medicine" is a question about the bytes on the page, which is what
validators actually have.

**Avoid thresholds the evidence will not state.** "Has substantial open source
experience" has no answer on a page. "The profile shows at least twenty merged pull
requests across the last twelve months" does, though note the contract cannot count for
you; a model will report what the page says and the grounding check will require it to
quote it.

**Write disqualifiers as presence tests.** They are answered with a boolean and any one
firing blocks access, so each should be one recognisable thing.

**Keep it small.** Eight conditions is the cap, and being near it is a warning sign. A
long condition list makes for a long prompt and a long response, and truncated model
output is a consensus problem rather than a quality problem.

## Evidence hosts

`allowed_hosts_json` restricts where evidence may come from. An entry matches the host
exactly or as a parent domain, so `github.com` admits `gist.github.com` and never
`evilgithub.com` or `github.com.attacker.net`. An empty list accepts any HTTPS host.

Set it. An open allowlist means an applicant can host their own evidence on a page they
control, which turns a credential check into a self declaration. Restrict to the
registries and authorities whose word you actually accept.

Only HTTPS is accepted anywhere. Plain HTTP is rejected at application time.

`fetch_mode` is fixed per gate. Use `"raw"` for a plain GET, which is right for APIs,
raw files and server rendered pages. Use `"render"` when the content only exists after
JavaScript runs. It cannot vary per application, because one node reading rendered text
while another reads raw HTML is a divergence.

Check a candidate URL before spending a bond:

```python
client.read_contract(
    address=REGISTRY, function_name="evidence_host_allowed",
    args=[GATE_ID, "https://medicalboard.example.gov/verify/884213"],
)
```

## Binding tokens

With `binding_required=True`, the evidence must contain the applicant's token:

```
glgate:<gate_id>:<applicant address, lowercase hex>
```

Fetch the exact string rather than building it yourself:

```python
token = client.read_contract(
    address=REGISTRY, function_name="binding_token",
    args=[GATE_ID, applicant_address],
)
```

The contract searches the fetched bytes for that string, case insensitively, before any
model call. The token names the address it authorises, so publishing someone else's
token proves nothing: an attacker applying from their own address needs their own token
on the page.

Require it whenever the policy is about the applicant. A licence record says someone is
licensed; it does not say that the wallet submitting it belongs to that person. Without
binding, anyone can submit any public credential.

Leave it off when the policy is about a document rather than a person, for example a
published sanctions list or a repository's own contents. There is nowhere to put a token
on a page you do not control.

In practice the applicant adds the token to a profile bio, a repository file, a gist, or
a personal site that the gate's allowlist accepts.

## Choosing the economic parameters

**`bond_wei`** is refunded on a grant and forfeited on a denial. Set it above the cost of
one adjudication so a failed application is not free, and below what an honest applicant
would find alarming. Zero is legitimate for a gate whose policy is a public good rather
than a contested resource.

**`challenge_stake_wei`** should be at least the bond. A challenger who wins recovers
their stake plus half the holder's deposit; one who loses forfeits the stake, half to the
holder. Setting it far below the bond makes nuisance challenges cheap.

**`access_ttl_seconds`** is how stale a grant may become. A licence revoked an hour after
a grant stays approved until the record expires, the owner revokes it, or someone
challenges it. Short lifetimes mean more adjudications and more cost; long ones mean more
drift. Ninety days suits a professional credential. An hour suits a session.

**`reapply_cooldown_seconds`** rate limits denied applicants. Without it, a rejected
address can retry immediately and repeatedly. A day is a reasonable default.

## The applicant flow

1. **Preflight.** `can_apply(gate_id, address)` returns the required bond, the current
   policy version, whether binding is required, the token to publish, and an `eligible`
   flag with a reason code. Call this before showing a form.
2. **Publish evidence.** If binding is required, the applicant puts the token on the
   document.
3. **Apply.** `apply_for_access(gate_id, evidence_urls_json, applicant_note)` with the
   bond attached as value. Returns the application id.
4. **Adjudicate.** `adjudicate(application_id)`. Callable by anyone, so a relayer or a
   keeper can drive it. This is the transaction that runs consensus, and it is the slow
   one.
5. **Read the outcome.** `get_application(application_id)` returns the decision, the
   denial code, the failing condition ids, and the per condition findings with their
   quotes.

The applicant note is a pointer, not evidence. It is passed to validators explicitly
labelled as an unverified claim.

### can_apply reasons

| Reason | Meaning |
|---|---|
| `OK` | Eligible |
| `UNKNOWN_GATE` | No such gate in this registry |
| `GATE_PAUSED` | Owner has paused intake; live records still work |
| `APPLICATION_PENDING` | One open application per address per gate |
| `ALREADY_APPROVED` | Already holds live access |
| `COOLDOWN_ACTIVE` | Denied recently; `cooldown_until` says when |

### Denial codes

| Code | What to tell the applicant |
|---|---|
| `EVIDENCE_UNRETRIEVABLE` | The document could not be fetched. Check the URL is public. |
| `BINDING_TOKEN_MISSING` | The wallet token is not on the document. |
| `DISQUALIFIER_PRESENT` | Something in the evidence blocks access. `failed_ids` says which. |
| `CONDITIONS_NOT_SATISFIED` | The evidence did not establish every condition. `failed_ids` says which. |

A condition can appear in `failed_ids` because it was genuinely not established, or
because the supporting quote could not be found in the document. The second case is
visible: the condition's `grounded` flag is false and its note begins with `Downgraded`.
Surface that difference. It usually means the evidence is fine and the model paraphrased.

### access_status reasons

| Reason | Meaning |
|---|---|
| `OK` | Approved |
| `NO_RECORD` | Never granted |
| `EXPIRED` | Lifetime elapsed |
| `POLICY_SUPERSEDED` | Granted under an older policy version; reapply |
| `REVOKED` | Closed by the gate owner or an upheld challenge |
| `RELEASED` | Given up voluntarily |

`POLICY_SUPERSEDED` is the one worth handling explicitly. The applicant did nothing
wrong; the rules changed.

## Policy updates

`update_policy` increments the gate's version and every outstanding record stops
counting immediately. One write, no iteration, no migration, whether the gate has three
holders or ten thousand.

This means a consumer contract inherits policy changes with no deployment. It also means
a gate owner can lock every holder out in one transaction, which is why the owner is part
of your trust model and why a gate serving an ecosystem should be owned by a DAO or a
multisig.

Use `update_gate_config` for bonds, lifetimes, hosts, fetch mode and cooldowns. It does
not bump the version, because none of those change what the policy requires. Use
`update_policy` when the requirements change, and expect every holder to reapply.

An application filed before an update and adjudicated after it is closed as `SUPERSEDED`
and refunded in full, never judged against conditions its applicant did not see.

## Off-chain reads

Any JSON-RPC client works. With `genlayer-py`:

```python
approved = client.read_contract(
    address=REGISTRY, function_name="is_approved", args=[GATE_ID, address]
)

status = json.loads(
    client.read_contract(
        address=REGISTRY, function_name="access_status", args=[GATE_ID, address]
    )
)
```

`is_approved` and `evidence_host_allowed` return booleans. Every other view returns a JSON
string, and returns an empty string for an object that does not exist, so a read never
reverts on a missing record. `genlayer-js` exposes the same methods for front ends.

Useful reads for an operator dashboard:

```python
json.loads(client.read_contract(address=REGISTRY, function_name="gate_stats", args=[GATE_ID]))
json.loads(client.read_contract(address=REGISTRY, function_name="list_holders", args=[GATE_ID, 0, 100]))
json.loads(client.read_contract(address=REGISTRY, function_name="list_applications", args=[GATE_ID, 0, 100]))
json.loads(client.read_contract(address=REGISTRY, function_name="get_registry_stats", args=[]))
```

`list_holders` returns every address ever granted, each with a `live` flag evaluated by
the same predicate `is_approved` uses, so a stale record is visibly stale rather than
silently counted.

## Challenging a record

```python
tx = client.write_contract(
    address=REGISTRY,
    function_name="challenge_access",
    args=[
        GATE_ID,
        holder_address,
        "Board record now shows a suspension against this licence",
        json.dumps(["https://medicalboard.example.gov/discipline/884213"]),
    ],
    value=challenge_stake_wei,
)
```

Then `resolve_challenge(challenge_id)`, callable by anyone. Resolution re-adjudicates the
holder's original evidence plus yours against the gate's current policy. Challenge
evidence is capped at two URLs because it is added to the holder's, and the combined
prompt has to stay within a reliable response budget.

Filing does not suspend access. What it does do is freeze the record: the holder cannot
release their deposit and the owner cannot revoke while a challenge is open, so neither
can change the stakes after you have staked.

## Operator checklist

- [ ] Pin `(registry address, gate_id)` at construction, never resolve from user input
- [ ] Pin the expected gate owner and expose a way to verify it
- [ ] Set `allowed_hosts_json` to the authorities you actually trust
- [ ] Set `binding_required=True` if the policy is about the applicant
- [ ] Leave `require_grounded_quotes=True` unless you have a specific reason
- [ ] Split conjunctions into separate conditions, and disqualifiers into presence tests
- [ ] Choose a TTL that bounds staleness for your risk, not for convenience
- [ ] Set the challenge stake at or above the bond
- [ ] Check receipts by reading the leader receipts, not the status name
- [ ] Handle `POLICY_SUPERSEDED` distinctly from a denial in your UI
- [ ] Transfer gate ownership to a multisig or DAO before others build on it

## Method reference

**Write**

```
register_gate(gate_id, title, policy_text, conditions_json, disqualifiers_json,
              allowed_hosts_json, fetch_mode, binding_required,
              require_grounded_quotes, access_ttl_seconds, bond_wei,
              challenge_stake_wei, reapply_cooldown_seconds) -> str
update_policy(gate_id, policy_text, conditions_json, disqualifiers_json) -> u32
update_gate_config(gate_id, allowed_hosts_json, fetch_mode, binding_required,
                   require_grounded_quotes, access_ttl_seconds, bond_wei,
                   challenge_stake_wei, reapply_cooldown_seconds) -> u32
set_gate_paused(gate_id, paused)
transfer_gate_ownership(gate_id, new_owner)
withdraw_treasury(gate_id, amount_wei) -> u256
apply_for_access(gate_id, evidence_urls_json, applicant_note) -> str   [payable]
adjudicate(application_id) -> str
claim_stale_application(application_id) -> u256
release_access(gate_id) -> u256
revoke_access(gate_id, holder, reason)
challenge_access(gate_id, holder, reason, evidence_urls_json) -> str   [payable]
resolve_challenge(challenge_id) -> str
```

**View**

```
is_approved(gate_id, subject) -> bool
access_status(gate_id, subject) -> str
binding_token(gate_id, subject) -> str
can_apply(gate_id, subject) -> str
evidence_host_allowed(gate_id, url) -> bool
get_gate(gate_id) -> str
get_policy(gate_id) -> str
get_application(application_id) -> str
get_access_record(gate_id, holder) -> str
get_challenge(challenge_id) -> str
list_gates(offset, limit) -> str
list_applications(gate_id, offset, limit) -> str
list_holders(gate_id, offset, limit) -> str
get_applicant_applications(applicant, offset, limit) -> str
gate_stats(gate_id) -> str
get_registry_stats() -> str
```
