# Semantic Access Gate

A GenLayer intelligent contract that enforces access policies written in plain English.

A gate owner states in ordinary language who may pass. Any address that wants access
submits a public evidence URL and a bond. GenLayer validators independently fetch the
evidence, adjudicate it against every condition in the policy, and agree or the
transaction does not commit. An approved address receives a time limited,
version stamped access record. Other contracts read that record through a single view
call, `is_approved(gate_id, address)`, and get a cached boolean.

Live on the GenLayer Studio network:

| | |
|---|---|
| Registry | [`0xE40dfb2befa0c643665568772C34eaaE852F9F62`](https://explorer-studio.genlayer.com/address/0xE40dfb2befa0c643665568772C34eaaE852F9F62) |
| Example consumer | [`0x65cA947f97175219f8c9692F1893864C019BF54A`](https://explorer-studio.genlayer.com/address/0x65cA947f97175219f8c9692F1893864C019BF54A) |
| Runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` |

`deployments/studionet.json` records the addresses, the deployment transactions and a
sha256 of each contract source, so the deployment can be checked against this repository:

```bash
sha256sum contracts/semantic_access_gate.py tests/fixtures/gated_consumer.py
```

## The problem

Smart contract access control is deterministic. You hold a token, or you are on a list,
or you are not. That covers a narrow band of the conditions people actually want to
express.

Many real access conditions are statements about the world:

- only licensed medical professionals
- only wallets with an active contribution record to open source projects
- only entities without published regulatory sanctions
- only accredited investors under a named jurisdiction's definition

None of these can be evaluated by code, because none of them are facts about chain
state. They are facts about documents, registries and public records. Protocols that
need such a condition have two options today. They can centralise the decision to an
admin who approves addresses by hand, which reintroduces exactly the trusted party the
protocol exists to remove. Or they can drop the condition and pretend the requirement
was not important.

This contract is the third option.

## What it does

The registry is permissionless. Anyone can register a gate. A gate is:

- a policy statement in plain English, which is what a prospective applicant reads
- a list of conditions, each of which the evidence must establish
- a list of disqualifiers, any one of which blocks access
- an allowlist of evidence hosts
- a bond, a challenge stake, an access lifetime and a reapply cooldown

An applicant submits one to four HTTPS URLs and the bond. Adjudication is a public
action that anyone can trigger. Validators fetch the documents, adjudicate every
condition independently, and the contract reduces their findings to a decision. A grant
produces an access record stamped with the gate's current eligibility versions and an
expiry.

The gate owner never approves anyone. There is no method that grants access directly.
The evidence and the policy do all the work.

## How consensus is used

Adjudication runs inside `gl.vm.run_nondet_unsafe` with a leader function and a
validator function. Both call the same module level function, `_run_adjudication`, over
the same frozen snapshot of the policy and the application.

### The round

The leader:

1. Fetches every evidence URL. HTTP status is classified, not ignored. A 2xx with a
   non empty body is retrievable. A 401, 403, 404, 410 or 451 is deterministically not
   retrievable, which every node sees identically. A 429 or a 5xx raises a
   `[TRANSIENT]` error.
2. Checks the binding token in code against the bytes it fetched, if the gate requires
   one.
3. Prompts its model for per condition findings. The prompt asks for verdicts and
   verbatim supporting quotes. It never asks whether to grant access.
4. Downgrades any satisfied verdict whose quote cannot be found in the fetched document.
5. Reduces the findings to a decision with a pure function.

Every validator runs the identical procedure. It fetches the evidence itself, prompts
its own model itself, and reduces its own findings itself.

### The three validator rules

**Rule 1, shape.** The leader's decision must be one of the two permitted values. A
payload that is not a dictionary, or that carries an unrecognised decision, is rejected.

**Rule 2, audit integrity.** The validator re-runs the reducer over the findings the
leader published and confirms they produce the decision the leader claimed, with the
same reason code. The per condition findings are written to chain as the permanent
justification for the access record. Without this rule a leader could publish a grant
alongside an audit trail recording a failed condition, and the on chain record would
contradict the access it justifies.

**Rule 3, independent adjudication.** The validator compares its own decision to the
leader's and agrees only if they match. It never inspects the leader's answer to decide
whether the answer looks plausible. This is the difference between verification and
rubber stamping.

### Why comparing one field is enough

The decision is computed by a pure function:

```
decision = GRANTED
    if evidence was retrievable
    and the binding check passed
    and no disqualifier is present
    and every condition is satisfied
```

Because a grant requires the conjunction to hold, agreement on `GRANTED` is
mathematically equivalent to agreement on every individual condition verdict. Any
validator that accepts a grant has independently found every condition satisfied. That
equivalence is what makes a one field comparison as strong as comparing the whole
finding set, and it is enumerated over the full input space in
`tests/unit/test_pure_logic.py::test_and_reduction_property_exhaustively`.

The deny path deliberately does not require agreement on reasons. Two honest validators
may fail an applicant on different conditions. Requiring them to agree on which
condition failed would deadlock adjudications that both nodes correctly want to deny.

### What is decided in code rather than by a model

Three things that a model must not be trusted with:

**The decision itself.** The model reports findings. The contract computes the outcome.
A model cannot produce a grant by asserting one, because there is no field in the
response schema that says whether access is granted.

**Proof of wallet control.** A gate can require the applicant to publish
`glgate:<gate_id>:<address>` on the evidence document. The contract searches the fetched
bytes for that exact string. The token names the address it authorises, so republishing
someone else's proof establishes nothing: an attacker applying from their own address
needs their own token on the page. This check runs before the model is consulted, and a
failure short circuits the round entirely.

**Grounding.** Every satisfied verdict must carry a span copied from the evidence. The
contract normalises both the quote and the document and requires the quote to appear in
the document. A verdict whose quote cannot be found is downgraded to not satisfied, with
the downgrade recorded in the audit trail. A model asserting a credential it did not
read does not produce access.

### Errors

Errors carry a classification prefix so the validator can decide agreement correctly:

| Prefix | Meaning | Validator behaviour |
|---|---|---|
| `[EXPECTED]` | Input or state rejected by the contract | Must match exactly |
| `[EXTERNAL]` | Deterministic upstream condition | Must match exactly |
| `[TRANSIENT]` | Outage, rate limit, 5xx | Both sides hitting one is agreement |
| `[LLM_ERROR]` | Unparseable model output | Never agreement, rotate the leader |

A transient fault reverts the transaction with no state change, so the bond is untouched
and the adjudication can simply be retried. A model fault rotates the leader rather than
writing a malformed verdict to chain.

## Versioning the eligibility rules

A gate's eligibility rules are not only its prose. Four mechanical settings decide who
qualifies just as directly as the conditions do: which hosts count as evidence, how that
evidence is fetched, whether the applicant must prove control of the wallet, and whether
a claim of compliance must be quotable from the fetched bytes. Relaxing the last two
removes the only deterministic checks standing between a document and a grant, so they
are versioned exactly as the policy text is.

Each gate therefore carries two counters:

| Counter | Bumped by | Means |
|---|---|---|
| `policy_version` | `update_policy` | what the gate requires |
| `rules_version` | `update_gate_config`, and only when one of the four adjudication-relevant settings actually changes | how the gate checks it |

Repricing a bond, extending a TTL or changing the cooldown moves neither counter, so a
gate can be repriced without revoking its holders. The comparison is made against the
canonicalized stored form, so reordering a host list is correctly a no-op rather than a
mass revocation.

Together the pair names one immutable **rules snapshot**, published on registration and
on every bump and never rewritten afterwards. `get_rules(gate_id, policy_version,
rules_version)` reads any of them back, which answers the question an auditor actually
has about a past decision: not what the gate requires now, but what it required when the
application was filed. Three guarantees follow, each in O(1):

**Existing grants are invalidated when either version moves.** `is_approved` compares
both stamps against the gate's current pair. That single write invalidates every
outstanding record at once — no loop over holders, no per record write, no migration, and
no gas cost proportional to the number of holders. A gate serving ten thousand addresses
is revised as cheaply as one serving three. Invalidation rewrites nothing: the record
stays `ACTIVE` with its old stamps, so the refusal stays legible. `access_status` reports
`POLICY_SUPERSEDED` or `RULES_SUPERSEDED` and names both pairs, and the holder's deposit
is still fully reclaimable through `release_access`.

**Pending applications cannot be judged under rules that moved.** Adjudication reads the
frame from the snapshot the application stamped, never from the live gate, and an
application whose gate has moved past either version closes as `SUPERSEDED` with a full
bond refund — no fetch, no model call. The owner has no write path to a published
snapshot, so no sequence of owner actions can judge a posted bond under rules it did not
agree to, in either direction: no tightening to seize an honest applicant's bond, and no
relaxing to let unqualified evidence through. The second direction is the security case
rather than a fairness one. Switching wallet binding and quote grounding off after a bond
is posted would otherwise let evidence carrying someone else's binding token, with quotes
appearing nowhere in the page, be adjudicated into a live grant — making the gate owner
able to grant access, which is the one thing this contract exists to prevent.

**Open challenges are not settled under rules that moved.** A challenge pins the record
and the version pair it was filed against. If any of the three has moved by the time it
resolves, it closes as `VOID` with the stake returned in full, because neither the
holder's deposit nor the challenger's stake was posted against the new frame.

The host allowlist is an adjudication rule, not merely an intake filter. A URL outside
the frozen frame's allowlist is marked inadmissible and never fetched, and the denial
carries its own code, `EVIDENCE_HOST_NOT_ALLOWED`.

## Crypto-economics

| Event | Applicant bond | Challenger stake |
|---|---|---|
| Granted | Becomes the record's deposit | |
| Denied | Forfeited to the gate treasury | |
| Superseded by a policy or rules change | Refunded in full | |
| Unadjudicated for seven days | Reclaimable by the applicant | |
| Access released or revoked | Deposit returned | |
| Granted again over a lapsed record | Replaced record's deposit refunded in the same transaction | |
| Challenge upheld | Deposit slashed, half to the challenger | Returned plus half the deposit |
| Challenge rejected | Deposit retained, half the stake as compensation | Slashed |
| Challenge voided by a rules change | Deposit untouched | Returned in full |

Denials cost money, so spamming a gate with unqualified applications is expensive while
honest applicants are left whole. A challenge is profitable only when it is correct.

Revocation by the gate owner returns the deposit, because revocation reflects new
information rather than a finding that the applicant lied. Punishing fraud is what
`challenge_access` is for.

Three paths exist so that money is never stranded. `claim_stale_application` lets an
applicant recover a bond nobody adjudicated after seven days, which is longer than any
plausible adjudication delay and so cannot be used to dodge an unfavourable verdict.
There is no arbitrary withdrawal.

A renewal settles the record it replaces. An access record lives at one slot per gate and
holder, so a holder whose record has lapsed — by expiry, or because a version bump
invalidated it — is granted into that same slot, and the record being replaced is still
`ACTIVE` and still holding the deposit that backed it. Writing over it would leave that
deposit inside `locked_wei` with no record pointing at it and no method able to reach it.
The grant therefore closes the prior record as `RENEWED`, archives it, and refunds its
deposit in the same transaction, reducing `locked_wei` by exactly the amount refunded.
The replaced record is preserved rather than destroyed and stays readable through
`get_access_history`, so the full sequence of grants an address has held remains
auditable. The refund is a refund and not a carry forward, so a renewed record holds
exactly one bond and the challenge payouts that split a deposit stay correct.

The contract tracks `locked_wei`, which is bonds on pending applications plus deposits
behind live records plus open challenge stakes, separately from `treasury_wei`, which is
what gate owners may withdraw. `withdraw_treasury` can only reach the latter.

## Challenges

An adjudication that was right in March may be wrong in September. Anyone can stake
against a live record and force a fresh consensus round over the holder's original
evidence plus the challenger's new evidence, judged against the gate's current policy.

Filing a challenge does not suspend access. Suspending on an unproven accusation would
make challenges a cheap denial of service against holders. What filing does do is lock
the record: a holder cannot release their collateral and walk away mid challenge, the
gate owner cannot rescue a holder by revoking first and returning the deposit, and the
holder cannot let the record lapse and renew it out from under the open challenge.

## Method surface

31 methods, 13 write and 18 view.

**Gate management:** `register_gate`, `update_policy`, `update_gate_config`,
`set_gate_paused`, `transfer_gate_ownership`, `withdraw_treasury`

**Applications:** `apply_for_access` (payable), `adjudicate`, `claim_stale_application`

**Access records:** `release_access`, `revoke_access`

**Challenges:** `challenge_access` (payable), `resolve_challenge`

**Composability:** `is_approved`, `access_status`, `binding_token`, `can_apply`,
`evidence_host_allowed`

**Reads:** `get_gate`, `get_policy`, `get_rules`, `get_application`,
`get_access_record`, `get_access_history`, `get_challenge`, `list_gates`,
`list_applications`, `list_holders`, `get_applicant_applications`, `gate_stats`,
`get_registry_stats`

`is_approved` and `evidence_host_allowed` return booleans. Every other view returns a JSON
string, and returns an empty string for an object that does not exist, so a front end never
has to catch a revert on a read. A view on an unknown gate fails closed: `is_approved`
returns false rather than raising.

## Tests

503 tests across three layers. Every one of them runs against the code that ships.

| Suite | Count | What it establishes |
|---|---|---|
| `tests/unit` | 156 | The consensus critical pure functions, driven directly |
| `tests/direct` | 308 | The whole contract inside the GenVM |
| `tests/live` | 39 | The deployed contract on StudioNet |

`tests/unit` and `tests/direct` must be run as two separate commands. Both directories
carry a `conftest.py` and the direct suites import theirs by module name, so collecting
both at once shadows it.

**Unit.** Direct mode executes the leader function only, so the validator half of a
consensus round cannot be reached there. These tests import the shipped contract module
outside the VM and drive its functions directly, substituting only the web and model
calls. Coverage includes the reducer truth table enumerated over every condition
pattern, the AND reduction property, host allowlist bypass attempts including suffix
lookalike domains, binding token address separation, model response parsing, HTTP status
classification, quote grounding, and the three validator rules including a leader that
forges a grant, a leader that forges a denial, and an audit trail that contradicts its
own decision. It also pins the evidence admissibility branch, which the frozen frame
makes unreachable from outside the contract and which is therefore exactly the kind of
defence in depth that rots untested.

**Direct.** The real contract inside the real GenVM, with only the evidence fetch and the
model call substituted. The validator closures are replayed through the harness, so the
rules are exercised as shipped. This layer covers registration and validation, intake
and bonds, every denial reason, policy and rules invalidation across holders at once, TTL
expiry, release, revocation, renewal settlement, all three challenge settlements, and an
accounting audit that recomputes the registry totals by walking the stored records after
every step of a scenario that interleaves every path across two gates and six addresses.

Two suites exist specifically to hold the lifecycle guarantees that money depends on.
`test_renewal_settlement.py` establishes that no sequence of lapse-and-renew can strand a
deposit, and asserts it the only way that cannot be faked: after any number of renewals
the registry still drains to zero. `test_rules_versioning.py` establishes that each of
the four adjudication-relevant settings is a versioned eligibility rule on its own, that
an economic edit is not, that a published frame is immutable, and that a pending
application is refunded rather than judged under a frame that moved — in both directions,
including the case where relaxing the frame would have let a gate owner manufacture a
grant.

**Live.** Real adjudications on StudioNet. Real validators fetch a real public document
over HTTPS, prompt real models, and consensus really has to agree. The suite proves a
grant on a real world document, a denial of the same document under a different policy,
the binding token path in both directions including an impersonation attempt, a dead link
resolving to a denial rather than an error, and the full composability story: an address is
granted access, is admitted by a separately deployed consumer contract, loses that
admission the moment the gate owner changes the policy, and regains it when the policy is
restored and the evidence is re-adjudicated. Two real adjudications also establish the
renewal path on chain: a grant is invalidated by a rules change, re-earned without being
released first, and the replaced deposit is refunded in the granting transaction, with
the registry's locked total returning to exactly where it started.

The live suite is deliberately a subset of the deterministic checks rather than a copy of
them. Every validation branch is already exercised against the same code inside the real
GenVM, and the Studio network enforces a request budget that receipt polling consumes
quickly, so re-running the whole validation matrix live would spend that budget without
adding signal.

### Running them

```bash
python -m venv .venv && . .venv/bin/activate
pip install genlayer-test genlayer-py pytest cloudpickle

pytest tests/unit                   # no network required
pytest tests/direct                 # no network required

export GENLAYER_DEPLOYER_KEY=0x...  # a funded StudioNet account that owns the gates
pytest tests/live/test_live_registry.py
pytest tests/live/test_live_consensus.py
```

Run the two live files as separate commands. StudioNet rate limits at thirty requests
per minute and receipt polling consumes that budget quickly, so collecting both at once
exhausts it and fails fixtures for reasons that have nothing to do with the contract.

The live suite skips entirely when `GENLAYER_DEPLOYER_KEY` is unset. No private key
appears anywhere in this repository or its history. Fund a StudioNet account with the
`sim_fundAccount` RPC method if its balance is zero.

Python 3.12 or newer is required for `tests/direct` and `tests/live`, because the current
`genlayer-test` release and the GenLayer SDK both use PEP 695 generics. `tests/unit` runs
on 3.10 and above.

Linting requires Python 3.12 or newer, because the GenLayer SDK uses PEP 695 generics:

```bash
pip install genvm-linter
genvm-lint check contracts/semantic_access_gate.py
```

## Deployment

```bash
export GENLAYER_DEPLOYER_KEY=0x...

python scripts/deploy.py                      # registry and example consumer
python scripts/deploy.py --registry 0x...     # consumer only, against an existing registry
```

The script writes `deployments/studionet.json`, which the live suite reads. It is written
outside `artifacts/` deliberately: the test plugin clears its artifacts directory at the
start of every session.

## Design decisions

**The fetch mode is fixed per gate.** Allowing a runtime fallback between a raw HTTPS GET
and headless rendering would let one node read rendered text while another reads raw
HTML. That is a divergence waiting to happen, so the mode is chosen once at registration.

**Conditions are capped at eight and evidence at four documents of four thousand
characters.** An unbounded policy grows the prompt until model output truncates, and a
truncated response is a consensus risk rather than a quality problem.

**Stored conditions are canonicalised.** They are sorted by id and re-serialised at
registration, because validators embed those exact bytes in their prompts and the stored
order must not depend on how the owner happened to type them.

**The adjudication closures capture a plain dictionary.** Capturing `self` would pull a
storage handle into a closure that has to serialise across a process boundary. The
snapshot is built once and copied in.

**Gate ids exclude the key separator.** Access records are keyed by gate id and holder
together. A separator inside a gate id would let one gate forge a key belonging to
another.

**Filing a challenge does not suspend access, and resolving one cannot be front run.**
Both follow from the same principle: an accusation is not a finding, and neither the
holder nor the gate owner should be able to change the stakes after one is filed.

**The eligibility frame is versioned and snapshotted, not just read live.** Versioning
alone would invalidate grants but still leave a pending application reading whatever the
gate says at adjudication time. Snapshotting alone would protect pending applications but
leave existing grants standing under requirements that no longer apply. The two defects
are different, so both mechanisms are present, and the snapshot is keyed by version pair
rather than copied onto every application: policy text runs to four thousand characters
and duplicating it per applicant would be a large cost for the same guarantee.

**A renewal refunds the replaced deposit rather than carrying it forward.** Carrying it
would make a record's deposit larger than one bond, and the challenge settlements split
a deposit, so the payout arithmetic would quietly drift from the stake that was actually
posted.

## Limitations

**A gate owner can rewrite the policy, and can change how it is checked.** That is the
point, and it is why every consumer should pin the gate owner it expects and expose a way
to verify it. The example consumer does this in `verify_gate_owner`. A gate meant to serve
an ecosystem should be owned by a DAO or a multisig, and `transfer_gate_ownership` exists
for that. What an owner cannot do is grant access: both kinds of change are versioned, so
they revoke rather than admit, and a pending application is refunded rather than
re-judged.

**Evidence must be publicly fetchable over HTTPS.** Validators cannot authenticate, so
anything behind a login is out of scope. A binding token proves control of the wallet,
not of the document's subject.

**Adjudication costs a model call per validator.** It is not a per transaction check. The
design assumes adjudication is rare and `is_approved` is frequent, which is why the
result is cached in a record and read as a boolean.

**Access lapses rather than following the world in real time.** A licence revoked an hour
after a grant stays approved until the record expires, the owner revokes it, or someone
challenges it. The TTL is the bound on that staleness, and it is per gate.

## Repository

```
contracts/semantic_access_gate.py   the primitive
tests/unit/                          consensus rules and pure functions
tests/direct/                        the contract inside the GenVM
tests/live/                          the deployed contract on StudioNet
tests/fixtures/gated_consumer.py     example consumer, deployed alongside
scripts/deploy.py                    deployment
deployments/studionet.json           live addresses
INTEGRATION.md                       how to build on it
```
