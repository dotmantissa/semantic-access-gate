# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""
Semantic Access Gate
====================

Purpose
-------
Access control in deterministic smart contracts can only express mechanical
predicates: hold this token, appear in this allowlist, pay this fee. A large
class of real access conditions is not mechanical at all:

  "only licensed medical professionals"
  "only wallets with a sustained public contribution record to open source"
  "only entities with no public regulatory sanctions"

None of those can be encoded as a predicate over on-chain state. Protocols that
need them either centralize the decision to an admin who manually approves
addresses, or drop the condition and accept the risk.

This contract is a permissionless registry of semantic access gates. A gate owner
publishes an access policy as natural language conditions stored on chain. Any
address that wants access submits public evidence URLs plus a bond. GenLayer
validators independently fetch that evidence and adjudicate whether it satisfies
every condition in the policy. An approved applicant receives a time limited,
policy-version-stamped access record. Any other contract reads that record with a
single call:

    gate.view().is_approved(gate_id, applicant) -> bool

The gate owner can never grant access. The owner writes the policy; the evidence
and the validators decide. This asymmetry is enforced by the contract: there is no
method that writes an ACTIVE access record outside of a consensus adjudication.

Why this needs GenLayer
-----------------------
Three properties are required at once, and no other execution environment
provides all three:

1. The judgment is subjective and evidence-based. Deciding whether a licensing
   board page actually establishes an active license, or whether a GitHub profile
   shows sustained contribution rather than a handful of typo fixes, requires
   language understanding over documents fetched from the open web.
2. The judgment must be verifiable by parties who do not trust each other. A
   centralized oracle that returns "approved" is exactly the admin key this
   primitive removes.
3. The judgment must be an authoritative on-chain state transition, because other
   contracts settle value against it.

GenLayer's Optimistic Democracy gives all three. Every validator independently
fetches the same evidence, independently runs its own language model over the same
policy, independently reduces its own findings to a decision, and the transaction
is only accepted if the validators' decisions match the leader's. Validators run
heterogeneous models, so a grant requires independent agreement across different
model families, not one provider's opinion.

Consensus design
----------------
Adjudication runs through gl.vm.run_nondet_unsafe with an explicit leader function
and validator function.

Leader:
  1. Fetches every evidence URL using the gate's pinned fetch mode.
  2. Runs the deterministic pre-checks in code, not in the model:
       - address binding: the gate's binding token, which encodes the applicant's
         own address, must appear verbatim in the fetched evidence;
       - retrievability: at least one evidence document must have been retrieved.
     A pre-check failure short-circuits to DENIED with a machine-readable denial
     code and never reaches the model.
  3. Prompts the model for a per-condition verdict plus a verbatim supporting
     quote for every condition it marks satisfied.
  4. Applies the deterministic grounding check in code: a satisfied condition
     whose quote cannot be found in the fetched evidence text is downgraded to
     not satisfied. The model cannot assert compliance it cannot quote.
  5. Reduces the per-condition verdicts to a decision with a pure function:

         decision = GRANTED  iff  every condition is satisfied
                                  and no disqualifier is present
                                  and binding passed
                                  and evidence was retrievable
                    DENIED   otherwise

Validator, in order:
  Rule 1 - Shape. The leader must return a decision of exactly GRANTED or DENIED.
  Rule 2 - Audit integrity. The validator re-reduces the leader's own per-condition
           findings through the same pure reducer and rejects if the result does not
           equal the decision the leader reported. The audit trail that gets written
           to chain therefore cannot misrepresent the decision it was accepted under.
  Rule 3 - Independent adjudication. The validator fetches the evidence itself,
           prompts its own model itself, reduces its own findings itself, and agrees
           only if its own decision equals the leader's decision.

The consensus rule is exact equality of the decision field. Because the decision is
a logical AND over the individual condition verdicts, exact agreement on a GRANTED
decision implies that every accepting validator independently found every single
condition satisfied. Agreement on the one-bit outcome is therefore not a weaker
check than per-condition agreement; on the grant path the two are equivalent. On
the deny path, nodes are allowed to deny for different reasons, which is what keeps
the gate from deadlocking on immaterial disagreement.

Error handling follows the standard classification. Transient fetch failures
(429, 5xx, render faults) raise a [TRANSIENT] error: leader and validator agree on
the failure, the transaction reverts, no state changes, and anyone can retry the
adjudication with the bond still escrowed. Deterministic fetch failures (401, 403,
404, 410) are not errors; they are evidence that the evidence is unretrievable and
resolve to DENIED.

Policy versioning
-----------------
Every gate carries a policy_version. Every access record is stamped with the
version it was adjudicated under. update_policy increments the version, which
invalidates every outstanding record in O(1): is_approved compares the record's
stamp with the gate's current version and returns False on mismatch. No mass
rewrite, no migration, no admin sweep. Holders must re-apply and be re-adjudicated
against the new standard. An application submitted under an older version is not
silently judged by the newer one either; it closes as SUPERSEDED with a full bond
refund, so nobody is judged against a policy they did not see.

Crypto-economics
----------------
The bond does three jobs and is fully accounted for at every step.

  apply_for_access  escrows bond_wei with the application.
  DENIED            the bond is slashed to the gate treasury. Spam costs money.
  GRANTED           the bond converts into the access record's stake and stays
                    locked for the life of the record.
  release_access    returns the stake to the holder and surrenders the access.
  challenge_access  anyone stakes an equal bond to force re-adjudication of a live
                    record against the current policy.
                      upheld   (re-adjudication denies) the record is revoked and
                               the holder's stake is paid to the challenger.
                      rejected (re-adjudication grants) the challenger's bond is
                               paid to the holder as compensation.
  revoke_access     the gate owner can revoke, but the stake returns to the holder.
                    The owner can deny access and can never profit from doing so.

Registry ownership
------------------
The registry itself has no privileged role. The deployer address is recorded for
provenance and holds no powers. Gate registration is permissionless and gate ids
are first come, first served, so integrators must pin both the registry address and
the gate id, and should verify the gate owner before deferring access decisions to
it.
"""

import json
import re
import typing
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# ---------------------------------------------------------------------------
# Error classification prefixes
# ---------------------------------------------------------------------------
# Deterministic errors must match exactly between leader and validator.
# Transient errors only need both sides to agree that a transient fault occurred.
# Model faults must never be agreed upon, so that consensus rotates the leader.

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

DECISION_GRANTED = "GRANTED"
DECISION_DENIED = "DENIED"

APP_PENDING = "PENDING"
APP_GRANTED = "GRANTED"
APP_DENIED = "DENIED"
APP_SUPERSEDED = "SUPERSEDED"
APP_EXPIRED_UNADJUDICATED = "STALE_REFUNDED"

ACCESS_ACTIVE = "ACTIVE"
ACCESS_REVOKED = "REVOKED"
ACCESS_RELEASED = "RELEASED"

CHALLENGE_OPEN = "OPEN"
CHALLENGE_UPHELD = "UPHELD"
CHALLENGE_REJECTED = "REJECTED"

VERDICT_SATISFIED = "SATISFIED"
VERDICT_NOT_SATISFIED = "NOT_SATISFIED"

DENIAL_NONE = ""
DENIAL_BINDING = "BINDING_TOKEN_MISSING"
DENIAL_UNRETRIEVABLE = "EVIDENCE_UNRETRIEVABLE"
DENIAL_CONDITIONS = "CONDITIONS_NOT_SATISFIED"
DENIAL_DISQUALIFIED = "DISQUALIFIER_PRESENT"
MALFORMED_ID = "<malformed>"

FETCH_RAW = "raw"
FETCH_RENDER = "render"

# Structural limits. These bound both on-chain storage cost and model prompt size,
# which keeps the adjudication output small enough that models do not truncate JSON.
MAX_CONDITIONS = 8
MAX_DISQUALIFIERS = 4
MAX_EVIDENCE_URLS = 4
MAX_CHALLENGE_URLS = 2
MAX_ALLOWED_HOSTS = 12
EVIDENCE_TEXT_LIMIT = 4000
MIN_CONDITION_TEXT = 12
MAX_CONDITION_TEXT = 400
MAX_POLICY_TEXT = 4000
MAX_NOTE_TEXT = 500
MIN_ACCESS_TTL_SECONDS = 60
MAX_ACCESS_TTL_SECONDS = 315360000
MAX_COOLDOWN_SECONDS = 31536000
MIN_GROUNDING_CHARS = 20

# An application that nobody adjudicates within this window can reclaim its bond.
# This exists so a bond can never be permanently stranded, while still making it
# impossible to withdraw an application in order to dodge a pending adjudication.
STALE_APPLICATION_SECONDS = 604800

GATE_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,47}$"
CONDITION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"

# Key separator for composite TreeMap keys. Excluded from gate ids and from the
# hex address form, so composite keys are unambiguous.
KEY_SEP = "|"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
# Everything in this section is a module level pure function. The adjudication
# closures reference these instead of contract methods so that the code executed
# by the leader and the code executed by every validator is byte-for-byte the same
# and captures no storage handle.


def _clean_json(raw: typing.Any) -> dict:
    """
    Coerce a model response into a JSON object.

    Models wrap JSON in prose, fence it in markdown, and leave trailing commas.
    This trims to the outermost brace pair and strips trailing commas before
    parsing. A response that still does not parse is an [LLM_ERROR], which
    validators never agree on, so consensus rotates the leader instead of writing
    a malformed verdict to chain.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        txt = raw.strip()
        first = txt.find("{")
        last = txt.rfind("}")
        if first >= 0 and last > first:
            txt = txt[first : last + 1]
        txt = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", txt)
        try:
            loaded = json.loads(txt)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_LLM} Model response was not parseable JSON")
        if isinstance(loaded, dict):
            return loaded
    raise gl.vm.UserError(f"{ERROR_LLM} Model response was not a JSON object")


def _normalize_text(value: str) -> str:
    """
    Collapse text to a comparison form: lowercase, alphanumeric runs separated by
    single spaces. Used for the grounding check so that a quote still matches when
    the model re-wraps whitespace or drops punctuation, while still requiring real
    content overlap with the fetched document.
    """
    lowered = str(value).lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return collapsed.strip()


def _normalize_verdict(value: typing.Any) -> str:
    """Map a model verdict onto the two allowed values, defaulting to not satisfied."""
    txt = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if txt in ("SATISFIED", "SATISFY", "MET", "PASS", "PASSED", "TRUE", "YES"):
        return VERDICT_SATISFIED
    return VERDICT_NOT_SATISFIED


def _coerce_flag(value: typing.Any) -> bool:
    """Coerce a model boolean, defaulting to False for anything unrecognized."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    txt = str(value).strip().lower()
    return txt in ("true", "yes", "1", "present", "detected")


def _host_of(url: str) -> str:
    """
    Extract the lowercase host from an https URL without importing a URL parser.

    Only https is accepted anywhere in this contract, so the scheme prefix is fixed
    and the host is everything up to the first '/', '?' or '#'. Userinfo ('@') and
    an explicit port are stripped so that 'https://user@github.com:443/x' and
    'https://github.com/x' resolve to the same host and cannot be used to slip past
    an allowlist.
    """
    txt = str(url).strip()
    lowered = txt.lower()
    if not lowered.startswith("https://"):
        return ""
    rest = txt[len("https://") :]
    for sep in ("/", "?", "#"):
        idx = rest.find(sep)
        if idx >= 0:
            rest = rest[:idx]
    if "@" in rest:
        rest = rest.split("@")[-1]
    if ":" in rest:
        rest = rest.split(":")[0]
    return rest.strip().lower()


def _host_allowed(host: str, allowed_hosts: list) -> bool:
    """
    Check a host against a gate allowlist.

    An empty allowlist accepts any host. An entry matches the host exactly or as a
    parent domain, so 'github.com' admits 'gist.github.com' but never 'evilgithub.com'.
    """
    if not allowed_hosts:
        return True
    if not host:
        return False
    for entry in allowed_hosts:
        allowed = str(entry).strip().lower().lstrip(".")
        if not allowed:
            continue
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _as_address(value: typing.Any) -> Address:
    """
    Normalize an address argument to an Address.

    Calldata decoding hands methods an Address, but a caller inside the VM can pass
    the raw 20 bytes, and the ABI boundary is not the place to raise an opaque
    AttributeError. Coercing once at method entry means every path downstream, the
    composite storage keys included, sees one representation.
    """
    if isinstance(value, Address):
        return value
    try:
        return Address(value)
    except Exception:
        raise gl.vm.UserError(f"{ERROR_EXPECTED} Value is not a valid address")


def _binding_token(gate_id: str, applicant_hex: str) -> str:
    """
    Build the proof-of-control token an applicant must publish on their evidence.

    The token embeds the applicant's own address, so republishing someone else's
    token proves nothing: an attacker applying from their own address needs their
    own token on the page. Combined with a gate's host allowlist, this is what
    separates "this document describes a licensed professional" from "this document
    describes the licensed professional who controls this wallet".
    """
    return "glgate:" + str(gate_id) + ":" + str(applicant_hex).strip().lower()


def _reduce_decision(
    condition_results: list,
    disqualifier_results: list,
    binding_ok: bool,
    evidence_ok: bool,
) -> tuple:
    """
    The consensus-critical reducer. Pure, total, and deterministic.

    Returns (decision, denial_code, failed_condition_ids).

    Both the leader and every validator run exactly this function over their own
    findings, and the validator additionally runs it over the leader's reported
    findings to confirm the leader's audit trail matches the decision it claimed.

    Because a grant requires every condition to be satisfied, agreement on a
    GRANTED decision is equivalent to agreement on every individual condition
    verdict. That equivalence is the security argument for comparing only the
    decision field across nodes.
    """
    # Order matters for the reason code, not the outcome. Retrievability is checked
    # first because the binding token is searched for inside the fetched bytes: with
    # nothing fetched, "token missing" would be a misleading thing to tell the
    # applicant when the real fault is a dead link.
    if not evidence_ok:
        return (DECISION_DENIED, DENIAL_UNRETRIEVABLE, [])
    if not binding_ok:
        return (DECISION_DENIED, DENIAL_BINDING, [])

    # A malformed entry is never read as a clean result. This matters because the
    # validator re-reduces the leader's reported findings, so this function must stay
    # total over arbitrary leader-supplied input and never raise.
    present_disqualifiers = []
    for item in disqualifier_results:
        if not isinstance(item, dict):
            present_disqualifiers.append(MALFORMED_ID)
        elif _coerce_flag(item.get("present", False)):
            present_disqualifiers.append(str(item.get("id", "")))
    if present_disqualifiers:
        return (DECISION_DENIED, DENIAL_DISQUALIFIED, present_disqualifiers)

    failed = []
    for item in condition_results:
        if not isinstance(item, dict):
            failed.append(MALFORMED_ID)
        elif _normalize_verdict(item.get("verdict")) != VERDICT_SATISFIED:
            failed.append(str(item.get("id", "")))
    if failed:
        return (DECISION_DENIED, DENIAL_CONDITIONS, failed)

    if not condition_results:
        # A gate with no conditions cannot grant anything. Registration rejects
        # empty policies, so this is a defensive floor rather than a reachable path.
        return (DECISION_DENIED, DENIAL_CONDITIONS, [])

    return (DECISION_GRANTED, DENIAL_NONE, [])


def _fetch_evidence(urls: list, fetch_mode: str) -> list:
    """
    Retrieve every evidence document inside the non-deterministic block.

    Status handling is what makes consensus stable:
      2xx                     retrievable
      401, 403, 404, 410, 451 deterministically not retrievable, both nodes see the
                              same thing, so this resolves to a DENIED decision
                              rather than an error
      429, 5xx                [TRANSIENT]: raised so the leader and the validator
                              agree on the fault, the transaction reverts, no state
                              changes, and the adjudication can simply be retried
    """
    documents = []
    for raw_url in urls:
        url = str(raw_url)
        if fetch_mode == FETCH_RENDER:
            try:
                rendered = gl.nondet.web.render(url, mode="text")
            except Exception as exc:
                raise gl.vm.UserError(
                    f"{ERROR_TRANSIENT} Evidence render failed for {url}: {exc}"
                )
            text = str(rendered)[:EVIDENCE_TEXT_LIMIT]
            documents.append(
                {
                    "url": url,
                    "status": 200,
                    "retrievable": len(text.strip()) > 0,
                    "text": text,
                }
            )
            continue

        try:
            response = gl.nondet.web.get(url)
        except Exception as exc:
            raise gl.vm.UserError(
                f"{ERROR_TRANSIENT} Evidence fetch failed for {url}: {exc}"
            )

        status = int(getattr(response, "status", 0) or 0)
        if status == 429 or status >= 500:
            raise gl.vm.UserError(
                f"{ERROR_TRANSIENT} Evidence source returned {status} for {url}"
            )

        body = getattr(response, "body", b"") or b""
        if isinstance(body, bytes):
            text = body.decode("utf-8", errors="replace")[:EVIDENCE_TEXT_LIMIT]
        else:
            text = str(body)[:EVIDENCE_TEXT_LIMIT]

        retrievable = 200 <= status < 300 and len(text.strip()) > 0
        documents.append(
            {
                "url": url,
                "status": status,
                "retrievable": retrievable,
                "text": text if retrievable else "",
            }
        )

    return documents


def _build_prompt(spec: dict, documents: list) -> str:
    """
    Compose the adjudication prompt.

    The prompt asks only for findings, never for the outcome. The decision itself is
    computed in code by _reduce_decision, so the model cannot return a grant by
    asserting one; it can only report per-condition verdicts that the reducer then
    combines. Every satisfied verdict must carry a verbatim quote, which the
    grounding check then verifies against the fetched text.
    """
    condition_lines = "\n".join(
        "- id=" + str(c["id"]) + " :: " + str(c["text"]) for c in spec["conditions"]
    )
    if spec["disqualifiers"]:
        disqualifier_lines = "\n".join(
            "- id=" + str(d["id"]) + " :: " + str(d["text"])
            for d in spec["disqualifiers"]
        )
    else:
        disqualifier_lines = "(none defined)"

    evidence_blocks = []
    for index, doc in enumerate(documents):
        if doc["retrievable"]:
            evidence_blocks.append(
                "=== EVIDENCE DOCUMENT "
                + str(index + 1)
                + " (url: "
                + str(doc["url"])
                + ", http status: "
                + str(doc["status"])
                + ") ===\n"
                + str(doc["text"])
            )
        else:
            evidence_blocks.append(
                "=== EVIDENCE DOCUMENT "
                + str(index + 1)
                + " (url: "
                + str(doc["url"])
                + ") COULD NOT BE RETRIEVED (http status: "
                + str(doc["status"])
                + ") ==="
            )
    evidence_text = "\n\n".join(evidence_blocks)

    return f"""You are an impartial access adjudicator for an on-chain access gate. You
examine public evidence documents and report, condition by condition, whether the
evidence establishes each condition. You do not decide whether access is granted;
a deterministic contract function combines your findings.

ACCESS POLICY (plain language statement of intent):
{spec["policy_text"]}

CONDITIONS. Every one of these must be established by the evidence:
{condition_lines}

DISQUALIFIERS. Report whether the evidence shows any of these:
{disqualifier_lines}

APPLICANT NOTE (unverified claim by the applicant, treat as a pointer only, never as proof):
{spec["applicant_note"]}

EVIDENCE DOCUMENTS FETCHED FROM THE PUBLIC WEB:
{evidence_text}

ADJUDICATION RULES:
1. Judge only what the evidence documents themselves establish. The applicant note
   is not evidence. Absence of information is NOT_SATISFIED, never SATISFIED.
2. For every condition marked SATISFIED you must supply "quote": a span copied
   VERBATIM from an evidence document above that establishes the condition. Copy the
   characters exactly. Do not paraphrase, summarize, translate or reformat the quote.
   A satisfied condition without a verbatim quote found in the documents is
   automatically downgraded to NOT_SATISFIED by the contract.
3. For a condition marked NOT_SATISFIED leave "quote" as an empty string.
4. Judge substance, not self-assertion. A page that merely claims a credential does
   not establish it unless the page itself is an authoritative record of it.
5. Be concise. "note" at most 15 words. "quote" at most 30 words. "reasoning" at
   most 60 words.

Respond with one JSON object and nothing else, in exactly this shape:
{{
  "conditions": [
    {{"id": "<condition id>", "verdict": "SATISFIED" or "NOT_SATISFIED", "quote": "<verbatim span or empty>", "note": "<short justification>"}}
  ],
  "disqualifiers": [
    {{"id": "<disqualifier id>", "present": true or false, "note": "<short justification>"}}
  ],
  "reasoning": "<short overall summary>"
}}
"""


def _run_adjudication(spec: dict) -> dict:
    """
    One complete, independent adjudication.

    The leader runs this. Every validator runs the identical function over its own
    fetches and its own model, then compares only the resulting decision. This is
    the independent-verification requirement: a validator never inspects the
    leader's answer to decide whether to agree, it forms its own.
    """
    documents = _fetch_evidence(spec["evidence_urls"], spec["fetch_mode"])

    combined_raw = "\n".join(doc["text"] for doc in documents if doc["retrievable"])
    combined_normalized = _normalize_text(combined_raw)
    evidence_ok = any(doc["retrievable"] for doc in documents)

    # Deterministic pre-check: proof of address control. Checked in code over the
    # bytes actually fetched, so it cannot be hallucinated or argued around.
    binding_ok = True
    if spec["binding_required"]:
        token = spec["binding_token"]
        binding_ok = token.lower() in combined_raw.lower()

    evidence_status = [
        {"url": doc["url"], "status": doc["status"], "retrievable": doc["retrievable"]}
        for doc in documents
    ]

    # Short-circuit: a failed pre-check is already a decision, so skip the model
    # entirely. This keeps denials for missing binding or dead links cheap and
    # perfectly reproducible across nodes.
    if not binding_ok or not evidence_ok:
        decision, denial_code, failed_ids = _reduce_decision(
            [], [], binding_ok, evidence_ok
        )
        return {
            "decision": decision,
            "denial_code": denial_code,
            "failed_ids": failed_ids,
            "binding_ok": binding_ok,
            "evidence_ok": evidence_ok,
            "conditions": [],
            "disqualifiers": [],
            "evidence_status": evidence_status,
            "reasoning": (
                "No evidence document could be retrieved."
                if not evidence_ok
                else "Binding token not found in the fetched evidence."
            ),
        }

    prompt = _build_prompt(spec, documents)
    parsed = _clean_json(gl.nondet.exec_prompt(prompt, response_format="json"))

    raw_conditions = parsed.get("conditions", [])
    by_id = {}
    if isinstance(raw_conditions, list):
        for item in raw_conditions:
            if isinstance(item, dict):
                by_id[str(item.get("id", "")).strip()] = item

    require_grounding = bool(spec["require_grounded_quotes"])
    condition_results = []
    for condition in spec["conditions"]:
        cid = str(condition["id"])
        reported = by_id.get(cid, {})
        verdict = _normalize_verdict(reported.get("verdict"))
        quote = str(reported.get("quote", ""))[:400]
        note = str(reported.get("note", ""))[:160]

        grounded = False
        if quote.strip():
            normalized_quote = _normalize_text(quote)
            grounded = (
                len(normalized_quote) >= MIN_GROUNDING_CHARS
                and normalized_quote in combined_normalized
            )

        # Deterministic anti-hallucination downgrade. A claim of compliance that
        # cannot be traced back to the fetched bytes is not a finding.
        if verdict == VERDICT_SATISFIED and require_grounding and not grounded:
            verdict = VERDICT_NOT_SATISFIED
            note = "Downgraded: supporting quote not found verbatim in evidence. " + note

        condition_results.append(
            {
                "id": cid,
                "verdict": verdict,
                "quote": quote,
                "grounded": grounded,
                "note": note[:200],
            }
        )

    raw_disqualifiers = parsed.get("disqualifiers", [])
    disq_by_id = {}
    if isinstance(raw_disqualifiers, list):
        for item in raw_disqualifiers:
            if isinstance(item, dict):
                disq_by_id[str(item.get("id", "")).strip()] = item

    disqualifier_results = []
    for disqualifier in spec["disqualifiers"]:
        did = str(disqualifier["id"])
        reported = disq_by_id.get(did, {})
        disqualifier_results.append(
            {
                "id": did,
                "present": _coerce_flag(reported.get("present", False)),
                "note": str(reported.get("note", ""))[:200],
            }
        )

    decision, denial_code, failed_ids = _reduce_decision(
        condition_results, disqualifier_results, binding_ok, evidence_ok
    )

    return {
        "decision": decision,
        "denial_code": denial_code,
        "failed_ids": failed_ids,
        "binding_ok": binding_ok,
        "evidence_ok": evidence_ok,
        "conditions": condition_results,
        "disqualifiers": disqualifier_results,
        "evidence_status": evidence_status,
        "reasoning": str(parsed.get("reasoning", ""))[:600],
    }


def _leader_audit_is_consistent(leader_output: dict) -> bool:
    """
    Validator rule 2: the leader's audit trail must reduce to the decision it claimed.

    The per-condition findings the leader reports are what gets written to chain as
    the permanent justification for the access record. Re-reducing them here means a
    leader cannot get a GRANTED decision accepted while publishing findings that say
    a condition failed, or publish a clean audit trail alongside a DENIED decision.
    """
    conditions = leader_output.get("conditions", [])
    disqualifiers = leader_output.get("disqualifiers", [])
    if not isinstance(conditions, list) or not isinstance(disqualifiers, list):
        return False

    expected_decision, expected_code, _failed = _reduce_decision(
        conditions,
        disqualifiers,
        bool(leader_output.get("binding_ok", False)),
        bool(leader_output.get("evidence_ok", False)),
    )
    if expected_decision != str(leader_output.get("decision", "")):
        return False
    return expected_code == str(leader_output.get("denial_code", ""))


def _validator_agrees_on_error(leaders_res: typing.Any, spec: dict) -> bool:
    """
    Decide agreement when the leader did not return a value.

    Deterministic errors ([EXPECTED], [EXTERNAL]) must match exactly. Transient
    errors only need both sides to have hit a transient fault, so a shared outage
    reverts the transaction cleanly instead of deadlocking it. A model fault, or a
    leader error the validator cannot reproduce, is a disagreement, which rotates
    the leader rather than committing a bad verdict.
    """
    leader_message = str(getattr(leaders_res, "message", ""))
    try:
        _run_adjudication(spec)
        return False
    except gl.vm.UserError as exc:
        validator_message = str(getattr(exc, "message", "") or exc)
        if validator_message.startswith(ERROR_EXPECTED) or validator_message.startswith(
            ERROR_EXTERNAL
        ):
            return validator_message == leader_message
        if validator_message.startswith(ERROR_TRANSIENT) and leader_message.startswith(
            ERROR_TRANSIENT
        ):
            return True
        return False
    except Exception:
        return False


def _adjudicate_with_consensus(spec: dict) -> dict:
    """
    Run one adjudication under GenLayer consensus and return the leader's verdict.

    The three validator rules are applied in order: shape, audit integrity,
    independent adjudication. Consensus succeeds only when the validator's own
    independently produced decision equals the leader's.
    """

    def leader_fn() -> dict:
        return _run_adjudication(spec)

    def validator_fn(leaders_res: typing.Any) -> bool:
        if not isinstance(leaders_res, gl.vm.Return):
            return _validator_agrees_on_error(leaders_res, spec)

        leader_output = getattr(leaders_res, "calldata", None)
        if not isinstance(leader_output, dict):
            return False

        # Rule 1: shape.
        leader_decision = str(leader_output.get("decision", ""))
        if leader_decision not in (DECISION_GRANTED, DECISION_DENIED):
            return False

        # Rule 2: audit integrity.
        if not _leader_audit_is_consistent(leader_output):
            return False

        # Rule 3: independent adjudication.
        try:
            own = _run_adjudication(spec)
        except Exception:
            return False

        return leader_decision == str(own["decision"])

    result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
    if not isinstance(result, dict) and hasattr(result, "get"):
        try:
            result = result.get()
        except TypeError:
            pass
    if not isinstance(result, dict):
        result = _clean_json(result)
    return result


# ---------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------


@allow_storage
@dataclass
class Gate:
    """
    One access gate: a natural language policy plus the machine-checkable frame
    validators adjudicate inside.
    """

    gate_id: str
    owner: Address
    title: str
    policy_text: str
    conditions_json: str
    disqualifiers_json: str
    allowed_hosts_json: str
    fetch_mode: str
    binding_required: bool
    require_grounded_quotes: bool
    policy_version: u32
    access_ttl_seconds: u64
    bond_wei: u256
    challenge_stake_wei: u256
    reapply_cooldown_seconds: u64
    paused: bool
    created_at: u64
    updated_at: u64
    total_applications: u32
    total_granted: u32
    total_denied: u32
    total_revoked: u32
    treasury_wei: u256


@allow_storage
@dataclass
class Application:
    """
    One request for access, its bond, and the full adjudication audit trail.
    """

    application_id: str
    gate_id: str
    applicant: Address
    evidence_urls_json: str
    applicant_note: str
    bond_wei: u256
    policy_version: u32
    status: str
    created_at: u64
    adjudicated_at: u64
    decision: str
    denial_code: str
    failed_ids_json: str
    conditions_json: str
    disqualifiers_json: str
    evidence_status_json: str
    reasoning: str
    binding_ok: bool
    evidence_ok: bool


@allow_storage
@dataclass
class AccessRecord:
    """
    A time-limited, version-stamped grant. This is the object is_approved reads.
    """

    gate_id: str
    holder: Address
    application_id: str
    policy_version: u32
    granted_at: u64
    expires_at: u64
    status: str
    deposit_wei: u256
    closed_at: u64
    close_reason: str
    challenge_count: u32


@allow_storage
@dataclass
class Challenge:
    """
    A staked claim that a live access record no longer satisfies the policy.
    """

    challenge_id: str
    gate_id: str
    holder: Address
    challenger: Address
    stake_wei: u256
    reason: str
    evidence_urls_json: str
    status: str
    created_at: u64
    resolved_at: u64
    outcome_decision: str
    outcome_denial_code: str
    outcome_reasoning: str


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class SemanticAccessGate(gl.Contract):
    """
    Semantic Access Gate: a permissionless registry of natural language access
    policies adjudicated by GenLayer validator consensus.

    Any address can register a gate. The gate owner writes a policy in plain
    English. Applicants submit public evidence URLs plus a bond. Validators fetch
    the evidence, adjudicate every condition independently, and the contract
    reduces their findings to a grant or a denial in deterministic code. Consumer
    contracts read one cached boolean through is_approved.
    """

    # Registry
    registry_owner: Address

    # Gates
    gates: TreeMap[str, Gate]
    gate_index: TreeMap[u32, str]
    gate_count: u32

    # Applications
    applications: TreeMap[str, Application]
    application_count: u64
    gate_application_count: TreeMap[str, u32]
    gate_applications: TreeMap[str, str]
    applicant_application_count: TreeMap[Address, u32]
    applicant_applications: TreeMap[str, str]
    open_application: TreeMap[str, str]
    last_denied_at: TreeMap[str, u64]

    # Access records, keyed by gate_id|holder_hex
    access: TreeMap[str, AccessRecord]
    gate_holder_count: TreeMap[str, u32]
    gate_holders: TreeMap[str, str]
    gate_holder_seen: TreeMap[str, bool]

    # Challenges
    challenges: TreeMap[str, Challenge]
    challenge_count: u64
    open_challenge: TreeMap[str, str]

    # Solvency accounting
    total_locked_wei: u256
    total_treasury_wei: u256

    def __init__(self) -> None:
        self.registry_owner = gl.message.sender_address
        self.gate_count = u32(0)
        self.application_count = u64(0)
        self.challenge_count = u64(0)
        self.total_locked_wei = u256(0)
        self.total_treasury_wei = u256(0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise gl.vm.UserError(message)

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

    def _gate_or_raise(self, gate_id: str) -> Gate:
        gate = self.gates.get(gate_id)
        self._require(gate is not None, f"{ERROR_EXPECTED} Unknown gate: {gate_id}")
        return gate

    def _require_gate_owner(self, gate: Gate) -> None:
        self._require(
            gl.message.sender_address == gate.owner,
            f"{ERROR_EXPECTED} Only the gate owner can perform this action",
        )

    def _access_key(self, gate_id: str, holder: typing.Any) -> str:
        return gate_id + KEY_SEP + _as_address(holder).as_hex.lower()

    def _parse_json_list(self, raw: str, label: str) -> list:
        try:
            parsed = json.loads(raw)
        except Exception:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} {label} must be valid JSON")
        self._require(
            isinstance(parsed, list), f"{ERROR_EXPECTED} {label} must be a JSON array"
        )
        return parsed

    def _validate_clauses(self, raw: str, label: str, limit: int, required: bool) -> str:
        """
        Validate and canonicalize a condition or disqualifier list.

        Canonicalization matters for consensus: the exact bytes stored here are the
        bytes every validator later reads and embeds in its prompt, so the stored
        form is sorted and re-serialized rather than kept as the caller typed it.
        """
        parsed = self._parse_json_list(raw, label)
        if required:
            self._require(len(parsed) >= 1, f"{ERROR_EXPECTED} {label} cannot be empty")
        self._require(
            len(parsed) <= limit,
            f"{ERROR_EXPECTED} {label} allows at most {limit} entries",
        )

        seen = set()
        clean = []
        for item in parsed:
            self._require(
                isinstance(item, dict),
                f"{ERROR_EXPECTED} Each entry in {label} must be a JSON object",
            )
            cid = str(item.get("id", "")).strip()
            text = " ".join(str(item.get("text", "")).split())
            self._require(
                re.match(CONDITION_ID_PATTERN, cid) is not None,
                f"{ERROR_EXPECTED} Invalid id in {label}: {cid}",
            )
            self._require(
                cid not in seen, f"{ERROR_EXPECTED} Duplicate id in {label}: {cid}"
            )
            seen.add(cid)
            self._require(
                MIN_CONDITION_TEXT <= len(text) <= MAX_CONDITION_TEXT,
                f"{ERROR_EXPECTED} Text for {cid} must be {MIN_CONDITION_TEXT} to {MAX_CONDITION_TEXT} characters",
            )
            clean.append({"id": cid, "text": text})

        clean.sort(key=lambda entry: entry["id"])
        return json.dumps(clean, sort_keys=True)

    def _validate_hosts(self, raw: str) -> str:
        parsed = self._parse_json_list(raw, "allowed_hosts_json")
        self._require(
            len(parsed) <= MAX_ALLOWED_HOSTS,
            f"{ERROR_EXPECTED} At most {MAX_ALLOWED_HOSTS} allowed hosts",
        )
        hosts = []
        for item in parsed:
            host = str(item).strip().lower().lstrip(".")
            self._require(
                re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", host) is not None,
                f"{ERROR_EXPECTED} Invalid host entry: {item}",
            )
            if host not in hosts:
                hosts.append(host)
        hosts.sort()
        return json.dumps(hosts, sort_keys=True)

    def _validate_evidence_urls(self, raw: str, gate: Gate) -> str:
        parsed = self._parse_json_list(raw, "evidence_urls_json")
        self._require(
            1 <= len(parsed) <= MAX_EVIDENCE_URLS,
            f"{ERROR_EXPECTED} Provide between 1 and {MAX_EVIDENCE_URLS} evidence URLs",
        )
        allowed_hosts = json.loads(gate.allowed_hosts_json)
        urls = []
        for item in parsed:
            url = str(item).strip()
            self._require(
                url.lower().startswith("https://"),
                f"{ERROR_EXPECTED} Evidence URL must use https: {url}",
            )
            self._require(
                len(url) <= 500, f"{ERROR_EXPECTED} Evidence URL is too long: {url}"
            )
            host = _host_of(url)
            self._require(
                host != "", f"{ERROR_EXPECTED} Evidence URL has no host: {url}"
            )
            self._require(
                _host_allowed(host, allowed_hosts),
                f"{ERROR_EXPECTED} Host not permitted by this gate: {host}",
            )
            if url not in urls:
                urls.append(url)
        return json.dumps(urls)

    def _build_spec(self, gate: Gate, application: Application) -> dict:
        """
        Freeze everything the adjudication needs into a plain dict.

        Nothing here is a storage handle. The dict is what gets captured by the
        leader and validator closures, so the adjudication runs over an immutable
        snapshot of the policy version the application was filed against.
        """
        return {
            "policy_text": str(gate.policy_text),
            "conditions": json.loads(str(gate.conditions_json)),
            "disqualifiers": json.loads(str(gate.disqualifiers_json)),
            "fetch_mode": str(gate.fetch_mode),
            "binding_required": bool(gate.binding_required),
            "require_grounded_quotes": bool(gate.require_grounded_quotes),
            "binding_token": _binding_token(
                str(gate.gate_id), application.applicant.as_hex
            ),
            "evidence_urls": json.loads(str(application.evidence_urls_json)),
            "applicant_note": str(application.applicant_note) or "(none provided)",
        }

    def _record_is_live(self, record: AccessRecord, gate: Gate, now: u64) -> bool:
        """
        The whole access check, in one place.

        A record is live only if it is ACTIVE, not expired, and stamped with the
        gate's current policy version. The version comparison is what makes policy
        updates invalidate every outstanding grant in O(1): the owner bumps one
        integer and every record issued under the old text stops reading as live,
        with no iteration and no migration.
        """
        if str(record.status) != ACCESS_ACTIVE:
            return False
        if int(record.expires_at) <= int(now):
            return False
        return int(record.policy_version) == int(gate.policy_version)

    def _close_record(
        self, key: str, record: AccessRecord, status: str, reason: str, now: u64
    ) -> None:
        record.status = status
        record.close_reason = reason[:MAX_NOTE_TEXT]
        record.closed_at = now
        self.access[key] = record

    # ------------------------------------------------------------------
    # Gate management
    # ------------------------------------------------------------------

    @gl.public.write
    def register_gate(
        self,
        gate_id: str,
        title: str,
        policy_text: str,
        conditions_json: str,
        disqualifiers_json: str,
        allowed_hosts_json: str,
        fetch_mode: str,
        binding_required: bool,
        require_grounded_quotes: bool,
        access_ttl_seconds: int,
        bond_wei: int,
        challenge_stake_wei: int,
        reapply_cooldown_seconds: int,
    ) -> str:
        """
        Register a new gate. Permissionless: the caller becomes the gate owner.

        Every consumer contract that defers to a gate must therefore verify the gate
        owner, because the gate_id namespace is open. Pin the pair
        (registry address, gate_id) and assert on gate.owner at integration time.

        Parameters
        ----------
        gate_id
            Lowercase slug, 3 to 48 characters, unique in this registry.
        policy_text
            The human readable statement of intent. This is what a prospective
            applicant reads, and it is included in the adjudication prompt.
        conditions_json
            JSON array of {"id", "text"}. Every condition must be established by the
            evidence for a grant. 1 to 8 entries.
        disqualifiers_json
            JSON array of {"id", "text"}. Any one present blocks a grant. 0 to 4.
        allowed_hosts_json
            JSON array of hostnames. Empty means any host is acceptable. An entry
            matches the host itself or any subdomain of it.
        fetch_mode
            "raw" for a plain HTTPS GET, "render" for headless text extraction of a
            JavaScript rendered page. Fixed per gate so leader and validators always
            retrieve evidence the same way.
        binding_required
            When true, the evidence must contain the applicant's binding token. This
            is what proves the wallet controls the credential rather than merely
            pointing at someone else's.
        require_grounded_quotes
            When true, a satisfied condition without a verbatim supporting quote
            found in the fetched bytes is downgraded to not satisfied in code.
        access_ttl_seconds
            Lifetime of a granted access record.
        bond_wei
            Bond an applicant must attach. Refunded on a grant, forfeited to the
            gate treasury on a denial.
        challenge_stake_wei
            Stake a challenger must attach to contest a live record.
        reapply_cooldown_seconds
            Minimum wait after a denial before the same address may apply again.
        """
        now = self._now()
        gid = gate_id.strip().lower()

        self._require(
            re.match(GATE_ID_PATTERN, gid) is not None,
            f"{ERROR_EXPECTED} gate_id must be 3 to 48 characters of a-z, 0-9, dot, dash or underscore",
        )
        self._require(
            self.gates.get(gid) is None,
            f"{ERROR_EXPECTED} Gate already registered: {gid}",
        )

        clean_title = " ".join(title.split())
        self._require(
            3 <= len(clean_title) <= 120,
            f"{ERROR_EXPECTED} title must be 3 to 120 characters",
        )

        clean_policy = policy_text.strip()
        self._require(
            40 <= len(clean_policy) <= MAX_POLICY_TEXT,
            f"{ERROR_EXPECTED} policy_text must be 40 to {MAX_POLICY_TEXT} characters",
        )

        mode = fetch_mode.strip().lower()
        self._require(
            mode in (FETCH_RAW, FETCH_RENDER),
            f"{ERROR_EXPECTED} fetch_mode must be 'raw' or 'render'",
        )

        self._require(
            MIN_ACCESS_TTL_SECONDS <= int(access_ttl_seconds) <= MAX_ACCESS_TTL_SECONDS,
            f"{ERROR_EXPECTED} access_ttl_seconds must be {MIN_ACCESS_TTL_SECONDS} to {MAX_ACCESS_TTL_SECONDS}",
        )
        self._require(
            int(bond_wei) >= 0, f"{ERROR_EXPECTED} bond_wei cannot be negative"
        )
        self._require(
            int(challenge_stake_wei) >= 0,
            f"{ERROR_EXPECTED} challenge_stake_wei cannot be negative",
        )
        self._require(
            0 <= int(reapply_cooldown_seconds) <= MAX_COOLDOWN_SECONDS,
            f"{ERROR_EXPECTED} reapply_cooldown_seconds must be 0 to {MAX_COOLDOWN_SECONDS}",
        )

        conditions = self._validate_clauses(
            conditions_json, "conditions_json", MAX_CONDITIONS, True
        )
        disqualifiers = self._validate_clauses(
            disqualifiers_json, "disqualifiers_json", MAX_DISQUALIFIERS, False
        )
        hosts = self._validate_hosts(allowed_hosts_json)

        self.gates[gid] = Gate(
            gate_id=gid,
            owner=gl.message.sender_address,
            title=clean_title,
            policy_text=clean_policy,
            conditions_json=conditions,
            disqualifiers_json=disqualifiers,
            allowed_hosts_json=hosts,
            fetch_mode=mode,
            binding_required=bool(binding_required),
            require_grounded_quotes=bool(require_grounded_quotes),
            policy_version=u32(1),
            access_ttl_seconds=u64(int(access_ttl_seconds)),
            bond_wei=u256(int(bond_wei)),
            challenge_stake_wei=u256(int(challenge_stake_wei)),
            reapply_cooldown_seconds=u64(int(reapply_cooldown_seconds)),
            paused=False,
            created_at=now,
            updated_at=now,
            total_applications=u32(0),
            total_granted=u32(0),
            total_denied=u32(0),
            total_revoked=u32(0),
            treasury_wei=u256(0),
        )

        self.gate_index[u32(int(self.gate_count))] = gid
        self.gate_count = u32(int(self.gate_count) + 1)
        return gid

    @gl.public.write
    def update_policy(
        self,
        gate_id: str,
        policy_text: str,
        conditions_json: str,
        disqualifiers_json: str,
    ) -> u32:
        """
        Replace the policy and bump the policy version.

        Every access record issued under an earlier version stops satisfying
        is_approved the instant this returns. No loop over holders is required: the
        records still exist, they are simply stamped with a version that no longer
        matches, so every read re-evaluates against the current policy and every
        holder must reapply. Returns the new version number.
        """
        gate = self._gate_or_raise(gate_id.strip().lower())
        self._require_gate_owner(gate)

        clean_policy = policy_text.strip()
        self._require(
            40 <= len(clean_policy) <= MAX_POLICY_TEXT,
            f"{ERROR_EXPECTED} policy_text must be 40 to {MAX_POLICY_TEXT} characters",
        )
        conditions = self._validate_clauses(
            conditions_json, "conditions_json", MAX_CONDITIONS, True
        )
        disqualifiers = self._validate_clauses(
            disqualifiers_json, "disqualifiers_json", MAX_DISQUALIFIERS, False
        )

        gate.policy_text = clean_policy
        gate.conditions_json = conditions
        gate.disqualifiers_json = disqualifiers
        gate.policy_version = u32(int(gate.policy_version) + 1)
        gate.updated_at = self._now()
        self.gates[gate.gate_id] = gate
        return gate.policy_version

    @gl.public.write
    def update_gate_config(
        self,
        gate_id: str,
        allowed_hosts_json: str,
        fetch_mode: str,
        binding_required: bool,
        require_grounded_quotes: bool,
        access_ttl_seconds: int,
        bond_wei: int,
        challenge_stake_wei: int,
        reapply_cooldown_seconds: int,
    ) -> u32:
        """
        Update the mechanical parameters of a gate without changing the policy text.

        These settings affect how evidence is retrieved and priced, not what the
        policy means, so the policy version is deliberately not bumped and existing
        grants stay live. Changing what the policy requires is update_policy, and
        that always invalidates.
        """
        gate = self._gate_or_raise(gate_id.strip().lower())
        self._require_gate_owner(gate)

        mode = fetch_mode.strip().lower()
        self._require(
            mode in (FETCH_RAW, FETCH_RENDER),
            f"{ERROR_EXPECTED} fetch_mode must be 'raw' or 'render'",
        )
        self._require(
            MIN_ACCESS_TTL_SECONDS <= int(access_ttl_seconds) <= MAX_ACCESS_TTL_SECONDS,
            f"{ERROR_EXPECTED} access_ttl_seconds must be {MIN_ACCESS_TTL_SECONDS} to {MAX_ACCESS_TTL_SECONDS}",
        )
        self._require(
            int(bond_wei) >= 0, f"{ERROR_EXPECTED} bond_wei cannot be negative"
        )
        self._require(
            int(challenge_stake_wei) >= 0,
            f"{ERROR_EXPECTED} challenge_stake_wei cannot be negative",
        )
        self._require(
            0 <= int(reapply_cooldown_seconds) <= MAX_COOLDOWN_SECONDS,
            f"{ERROR_EXPECTED} reapply_cooldown_seconds must be 0 to {MAX_COOLDOWN_SECONDS}",
        )

        gate.allowed_hosts_json = self._validate_hosts(allowed_hosts_json)
        gate.fetch_mode = mode
        gate.binding_required = bool(binding_required)
        gate.require_grounded_quotes = bool(require_grounded_quotes)
        gate.access_ttl_seconds = u64(int(access_ttl_seconds))
        gate.bond_wei = u256(int(bond_wei))
        gate.challenge_stake_wei = u256(int(challenge_stake_wei))
        gate.reapply_cooldown_seconds = u64(int(reapply_cooldown_seconds))
        gate.updated_at = self._now()
        self.gates[gate.gate_id] = gate
        return gate.policy_version

    @gl.public.write
    def set_gate_paused(self, gate_id: str, paused: bool) -> None:
        """
        Pause or resume new applications.

        Pausing stops intake only. Live access records keep working and pending
        applications can still be adjudicated, so a pause never strands a bond and
        never silently cuts off an ecosystem of consumer contracts mid-flight.
        """
        gate = self._gate_or_raise(gate_id.strip().lower())
        self._require_gate_owner(gate)
        gate.paused = bool(paused)
        gate.updated_at = self._now()
        self.gates[gate.gate_id] = gate

    @gl.public.write
    def transfer_gate_ownership(self, gate_id: str, new_owner: Address) -> None:
        """Hand a gate to a new owner, for example a DAO or multisig."""
        gate = self._gate_or_raise(gate_id.strip().lower())
        self._require_gate_owner(gate)
        owner = _as_address(new_owner)
        self._require(
            owner.as_hex.lower() != "0x" + "0" * 40,
            f"{ERROR_EXPECTED} new_owner cannot be the zero address",
        )
        gate.owner = owner
        gate.updated_at = self._now()
        self.gates[gate.gate_id] = gate

    @gl.public.write
    def withdraw_treasury(self, gate_id: str, amount_wei: int) -> u256:
        """
        Withdraw forfeited bonds and slashed stakes accrued to this gate.

        Only the gate treasury is withdrawable. Bonds on pending applications and
        deposits behind live records are tracked in total_locked_wei and can never be
        reached from here, which is the contract's solvency invariant.
        """
        gate = self._gate_or_raise(gate_id.strip().lower())
        self._require_gate_owner(gate)
        amount = int(amount_wei)
        self._require(amount > 0, f"{ERROR_EXPECTED} amount_wei must be positive")
        self._require(
            amount <= int(gate.treasury_wei),
            f"{ERROR_EXPECTED} Amount exceeds gate treasury of {int(gate.treasury_wei)} wei",
        )

        gate.treasury_wei = u256(int(gate.treasury_wei) - amount)
        self.gates[gate.gate_id] = gate
        self.total_treasury_wei = u256(int(self.total_treasury_wei) - amount)

        gl.get_contract_at(gate.owner).emit_transfer(
            value=u256(amount), on="finalized"
        )
        return gate.treasury_wei

    # ------------------------------------------------------------------
    # Application lifecycle
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def apply_for_access(
        self, gate_id: str, evidence_urls_json: str, applicant_note: str
    ) -> str:
        """
        Submit evidence and a bond, requesting access under the gate's current policy.

        The bond is escrowed, not spent. It comes back in full on a grant. It is
        forfeited to the gate treasury on a denial, which is what makes spamming a
        gate with unqualified applications cost money while leaving honest applicants
        whole.

        The application is stamped with the policy version in force right now. If the
        owner changes the policy before adjudication, the application is closed and
        fully refunded rather than judged against text the applicant never saw.
        """
        gid = gate_id.strip().lower()
        gate = self._gate_or_raise(gid)
        applicant = gl.message.sender_address
        now = self._now()

        self._require(
            not bool(gate.paused),
            f"{ERROR_EXPECTED} Gate is paused and is not accepting applications",
        )

        sent = int(gl.message.value)
        self._require(
            sent == int(gate.bond_wei),
            f"{ERROR_EXPECTED} Bond must be exactly {int(gate.bond_wei)} wei, received {sent}",
        )

        holder_key = self._access_key(gid, applicant)
        self._require(
            self.open_application.get(holder_key) is None,
            f"{ERROR_EXPECTED} An application is already pending for this address on this gate",
        )

        existing = self.access.get(holder_key)
        if existing is not None and self._record_is_live(existing, gate, now):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Address already holds live access under policy version {int(gate.policy_version)}"
            )

        cooldown = int(gate.reapply_cooldown_seconds)
        if cooldown > 0:
            last_denied = self.last_denied_at.get(holder_key)
            if last_denied is not None and int(last_denied) > 0:
                ready_at = int(last_denied) + cooldown
                self._require(
                    int(now) >= ready_at,
                    f"{ERROR_EXPECTED} Reapply cooldown active until unix time {ready_at}",
                )

        clean_note = " ".join(applicant_note.split())[:MAX_NOTE_TEXT]
        urls = self._validate_evidence_urls(evidence_urls_json, gate)

        application_id = "app_" + str(int(self.application_count))
        self.applications[application_id] = Application(
            application_id=application_id,
            gate_id=gid,
            applicant=applicant,
            evidence_urls_json=urls,
            applicant_note=clean_note,
            bond_wei=u256(sent),
            policy_version=gate.policy_version,
            status=APP_PENDING,
            created_at=now,
            adjudicated_at=u64(0),
            decision="",
            denial_code="",
            failed_ids_json="[]",
            conditions_json="[]",
            disqualifiers_json="[]",
            evidence_status_json="[]",
            reasoning="",
            binding_ok=False,
            evidence_ok=False,
        )
        self.application_count = u64(int(self.application_count) + 1)
        self.open_application[holder_key] = application_id

        gate_seq = int(self.gate_application_count.get(gid) or 0)
        self.gate_applications[gid + KEY_SEP + str(gate_seq)] = application_id
        self.gate_application_count[gid] = u32(gate_seq + 1)

        applicant_seq = int(self.applicant_application_count.get(applicant) or 0)
        self.applicant_applications[
            applicant.as_hex.lower() + KEY_SEP + str(applicant_seq)
        ] = application_id
        self.applicant_application_count[applicant] = u32(applicant_seq + 1)

        gate.total_applications = u32(int(gate.total_applications) + 1)
        self.gates[gid] = gate
        self.total_locked_wei = u256(int(self.total_locked_wei) + sent)

        return application_id

    @gl.public.write
    def adjudicate(self, application_id: str) -> str:
        """
        Run consensus adjudication on a pending application and settle it.

        Callable by anyone. Adjudication is a public good: the applicant wants their
        answer, the gate owner wants the treasury credit, and a consumer contract may
        want a queue cleared, so no single party can stall the gate by refusing to
        act.

        Validators independently fetch the evidence, prompt their own models, and
        reduce their own findings. The transaction commits only when the validator's
        own decision matches the leader's. Returns the adjudication result as JSON.
        """
        application = self.applications.get(application_id)
        self._require(
            application is not None,
            f"{ERROR_EXPECTED} Unknown application: {application_id}",
        )
        self._require(
            str(application.status) == APP_PENDING,
            f"{ERROR_EXPECTED} Application is already {str(application.status)}",
        )

        gate = self._gate_or_raise(str(application.gate_id))
        now = self._now()
        holder_key = self._access_key(str(application.gate_id), application.applicant)
        bond = int(application.bond_wei)

        # Mid-flight policy change: close and refund rather than judge the applicant
        # against conditions they never agreed to. This keeps the gate honest and
        # keeps the version stamp meaningful.
        if int(application.policy_version) != int(gate.policy_version):
            application.status = APP_SUPERSEDED
            application.adjudicated_at = now
            application.decision = ""
            application.denial_code = ""
            application.reasoning = (
                "Policy changed from version "
                + str(int(application.policy_version))
                + " to version "
                + str(int(gate.policy_version))
                + " before adjudication. Bond refunded in full; reapply under the new policy."
            )
            self.applications[application_id] = application
            if self.open_application.get(holder_key) is not None:
                del self.open_application[holder_key]
            self.total_locked_wei = u256(int(self.total_locked_wei) - bond)
            if bond > 0:
                gl.get_contract_at(application.applicant).emit_transfer(
                    value=u256(bond), on="finalized"
                )
            return json.dumps(
                {
                    "application_id": application_id,
                    "status": APP_SUPERSEDED,
                    "decision": "",
                    "refunded_wei": str(bond),
                    "reason": str(application.reasoning),
                },
                sort_keys=True,
            )

        spec = self._build_spec(gate, application)
        result = _adjudicate_with_consensus(spec)

        decision = str(result.get("decision", DECISION_DENIED))
        if decision not in (DECISION_GRANTED, DECISION_DENIED):
            decision = DECISION_DENIED

        application.adjudicated_at = now
        application.decision = decision
        application.denial_code = str(result.get("denial_code", ""))[:64]
        application.failed_ids_json = json.dumps(
            [str(x) for x in result.get("failed_ids", [])], sort_keys=True
        )
        application.conditions_json = json.dumps(
            result.get("conditions", []), sort_keys=True
        )
        application.disqualifiers_json = json.dumps(
            result.get("disqualifiers", []), sort_keys=True
        )
        application.evidence_status_json = json.dumps(
            result.get("evidence_status", []), sort_keys=True
        )
        application.reasoning = str(result.get("reasoning", ""))[:600]
        application.binding_ok = bool(result.get("binding_ok", False))
        application.evidence_ok = bool(result.get("evidence_ok", False))

        if self.open_application.get(holder_key) is not None:
            del self.open_application[holder_key]

        if decision == DECISION_GRANTED:
            application.status = APP_GRANTED
            expires_at = u64(int(now) + int(gate.access_ttl_seconds))
            self.access[holder_key] = AccessRecord(
                gate_id=str(application.gate_id),
                holder=application.applicant,
                application_id=application_id,
                policy_version=gate.policy_version,
                granted_at=now,
                expires_at=expires_at,
                status=ACCESS_ACTIVE,
                deposit_wei=u256(bond),
                closed_at=u64(0),
                close_reason="",
                challenge_count=u32(0),
            )
            if self.gate_holder_seen.get(holder_key) is None:
                seq = int(self.gate_holder_count.get(str(application.gate_id)) or 0)
                self.gate_holders[
                    str(application.gate_id) + KEY_SEP + str(seq)
                ] = application.applicant.as_hex
                self.gate_holder_count[str(application.gate_id)] = u32(seq + 1)
                self.gate_holder_seen[holder_key] = True
            gate.total_granted = u32(int(gate.total_granted) + 1)
        else:
            application.status = APP_DENIED
            self.last_denied_at[holder_key] = now
            gate.total_denied = u32(int(gate.total_denied) + 1)
            if bond > 0:
                gate.treasury_wei = u256(int(gate.treasury_wei) + bond)
                self.total_treasury_wei = u256(int(self.total_treasury_wei) + bond)
                self.total_locked_wei = u256(int(self.total_locked_wei) - bond)

        self.applications[application_id] = application
        self.gates[str(application.gate_id)] = gate

        return json.dumps(
            {
                "application_id": application_id,
                "gate_id": str(application.gate_id),
                "applicant": application.applicant.as_hex,
                "policy_version": int(application.policy_version),
                "status": str(application.status),
                "decision": decision,
                "denial_code": str(application.denial_code),
                "failed_ids": json.loads(str(application.failed_ids_json)),
                "binding_ok": bool(application.binding_ok),
                "evidence_ok": bool(application.evidence_ok),
                "conditions": json.loads(str(application.conditions_json)),
                "disqualifiers": json.loads(str(application.disqualifiers_json)),
                "evidence_status": json.loads(str(application.evidence_status_json)),
                "reasoning": str(application.reasoning),
            },
            sort_keys=True,
        )

    @gl.public.write
    def claim_stale_application(self, application_id: str) -> u256:
        """
        Reclaim a bond from an application nobody adjudicated.

        Adjudication depends on a live validator set and a working evidence host.
        Without this path a permanent outage would strand an applicant's bond
        forever. After STALE_APPLICATION_SECONDS the applicant can close their own
        application and take the bond back. There is no arbitrary withdrawal, so this
        cannot be used to dodge an unfavourable verdict: the window is longer than
        any realistic adjudication delay.
        """
        application = self.applications.get(application_id)
        self._require(
            application is not None,
            f"{ERROR_EXPECTED} Unknown application: {application_id}",
        )
        self._require(
            str(application.status) == APP_PENDING,
            f"{ERROR_EXPECTED} Application is already {str(application.status)}",
        )
        self._require(
            gl.message.sender_address == application.applicant,
            f"{ERROR_EXPECTED} Only the applicant can reclaim this bond",
        )

        now = self._now()
        ready_at = int(application.created_at) + STALE_APPLICATION_SECONDS
        self._require(
            int(now) >= ready_at,
            f"{ERROR_EXPECTED} Bond is reclaimable from unix time {ready_at}",
        )

        bond = int(application.bond_wei)
        application.status = APP_EXPIRED_UNADJUDICATED
        application.adjudicated_at = now
        application.reasoning = (
            "Application was not adjudicated within the stale window. Bond refunded."
        )
        self.applications[application_id] = application

        holder_key = self._access_key(str(application.gate_id), application.applicant)
        if self.open_application.get(holder_key) is not None:
            del self.open_application[holder_key]

        self.total_locked_wei = u256(int(self.total_locked_wei) - bond)
        if bond > 0:
            gl.get_contract_at(application.applicant).emit_transfer(
                value=u256(bond), on="finalized"
            )
        return u256(bond)

    # ------------------------------------------------------------------
    # Access record lifecycle
    # ------------------------------------------------------------------

    @gl.public.write
    def release_access(self, gate_id: str) -> u256:
        """
        Voluntarily give up access and reclaim the bond behind it.

        The bond sits behind a live record as collateral for a challenge. Releasing
        access returns it. A record with an open challenge cannot be released, so a
        holder cannot dodge a challenge by walking away with the deposit.
        """
        gid = gate_id.strip().lower()
        gate = self._gate_or_raise(gid)
        holder = gl.message.sender_address
        key = self._access_key(gid, holder)

        record = self.access.get(key)
        self._require(
            record is not None, f"{ERROR_EXPECTED} No access record for this address"
        )
        self._require(
            str(record.status) == ACCESS_ACTIVE,
            f"{ERROR_EXPECTED} Access record is already {str(record.status)}",
        )
        self._require(
            self.open_challenge.get(key) is None,
            f"{ERROR_EXPECTED} Cannot release access while a challenge is open",
        )

        now = self._now()
        deposit = int(record.deposit_wei)
        record.deposit_wei = u256(0)
        self._close_record(key, record, ACCESS_RELEASED, "Released by holder", now)

        self.total_locked_wei = u256(int(self.total_locked_wei) - deposit)
        if deposit > 0:
            gl.get_contract_at(holder).emit_transfer(
                value=u256(deposit), on="finalized"
            )
        return u256(deposit)

    @gl.public.write
    def revoke_access(self, gate_id: str, holder: Address, reason: str) -> None:
        """
        Owner revocation for out of band facts the policy cannot see.

        A licence can be suspended and a sanction can be published between
        adjudications. The bond is returned, because revocation is not a finding that
        the applicant lied; it reflects new information. Fraud is punished through
        challenge_access, which slashes.
        """
        gid = gate_id.strip().lower()
        gate = self._gate_or_raise(gid)
        self._require_gate_owner(gate)

        key = self._access_key(gid, holder)
        record = self.access.get(key)
        self._require(
            record is not None, f"{ERROR_EXPECTED} No access record for this address"
        )
        self._require(
            str(record.status) == ACCESS_ACTIVE,
            f"{ERROR_EXPECTED} Access record is already {str(record.status)}",
        )
        self._require(
            self.open_challenge.get(key) is None,
            f"{ERROR_EXPECTED} Cannot revoke while a challenge is open; resolve it first",
        )

        now = self._now()
        deposit = int(record.deposit_wei)
        record.deposit_wei = u256(0)
        self._close_record(
            key,
            record,
            ACCESS_REVOKED,
            "Revoked by gate owner: " + " ".join(reason.split()),
            now,
        )

        gate.total_revoked = u32(int(gate.total_revoked) + 1)
        self.gates[gid] = gate

        self.total_locked_wei = u256(int(self.total_locked_wei) - deposit)
        if deposit > 0:
            gl.get_contract_at(holder).emit_transfer(
                value=u256(deposit), on="finalized"
            )

    # ------------------------------------------------------------------
    # Challenges
    # ------------------------------------------------------------------

    def _validate_challenge_urls(self, raw: str, gate: Gate) -> str:
        parsed = self._parse_json_list(raw, "evidence_urls_json")
        self._require(
            1 <= len(parsed) <= MAX_CHALLENGE_URLS,
            f"{ERROR_EXPECTED} Provide between 1 and {MAX_CHALLENGE_URLS} challenge evidence URLs",
        )
        allowed_hosts = json.loads(gate.allowed_hosts_json)
        urls = []
        for item in parsed:
            url = str(item).strip()
            self._require(
                url.lower().startswith("https://"),
                f"{ERROR_EXPECTED} Challenge evidence URL must use https: {url}",
            )
            self._require(
                len(url) <= 500, f"{ERROR_EXPECTED} Challenge evidence URL is too long"
            )
            host = _host_of(url)
            self._require(
                host != "", f"{ERROR_EXPECTED} Challenge evidence URL has no host: {url}"
            )
            self._require(
                _host_allowed(host, allowed_hosts),
                f"{ERROR_EXPECTED} Host not permitted by this gate: {host}",
            )
            if url not in urls:
                urls.append(url)
        return json.dumps(urls)

    @gl.public.write.payable
    def challenge_access(
        self, gate_id: str, holder: Address, reason: str, evidence_urls_json: str
    ) -> str:
        """
        Stake against a live access record and force a re-adjudication.

        This is the permissionless correction path. Nobody has to trust that the
        original adjudication was right forever, and nobody has to trust the gate
        owner to police their own gate. Anyone who can point at public evidence that
        a holder no longer satisfies the policy can put money behind that claim.

        The stake is escrowed. It comes back plus a share of the holder's slashed
        deposit if the challenge is upheld, and is slashed if it is not, so a
        challenge is only profitable when it is correct.
        """
        gid = gate_id.strip().lower()
        gate = self._gate_or_raise(gid)
        challenger = gl.message.sender_address
        now = self._now()
        key = self._access_key(gid, holder)

        record = self.access.get(key)
        self._require(
            record is not None, f"{ERROR_EXPECTED} No access record for this address"
        )
        self._require(
            self._record_is_live(record, gate, now),
            f"{ERROR_EXPECTED} Access record is not live and does not need challenging",
        )
        self._require(
            challenger != holder,
            f"{ERROR_EXPECTED} A holder cannot challenge their own access; use release_access",
        )
        self._require(
            self.open_challenge.get(key) is None,
            f"{ERROR_EXPECTED} A challenge is already open against this holder",
        )

        sent = int(gl.message.value)
        self._require(
            sent == int(gate.challenge_stake_wei),
            f"{ERROR_EXPECTED} Stake must be exactly {int(gate.challenge_stake_wei)} wei, received {sent}",
        )

        clean_reason = " ".join(reason.split())
        self._require(
            10 <= len(clean_reason) <= MAX_NOTE_TEXT,
            f"{ERROR_EXPECTED} reason must be 10 to {MAX_NOTE_TEXT} characters",
        )
        urls = self._validate_challenge_urls(evidence_urls_json, gate)

        challenge_id = "chal_" + str(int(self.challenge_count))
        self.challenges[challenge_id] = Challenge(
            challenge_id=challenge_id,
            gate_id=gid,
            holder=holder,
            challenger=challenger,
            stake_wei=u256(sent),
            reason=clean_reason,
            evidence_urls_json=urls,
            status=CHALLENGE_OPEN,
            created_at=now,
            resolved_at=u64(0),
            outcome_decision="",
            outcome_denial_code="",
            outcome_reasoning="",
        )
        self.challenge_count = u64(int(self.challenge_count) + 1)
        self.open_challenge[key] = challenge_id

        record.challenge_count = u32(int(record.challenge_count) + 1)
        self.access[key] = record

        self.total_locked_wei = u256(int(self.total_locked_wei) + sent)
        return challenge_id

    @gl.public.write
    def resolve_challenge(self, challenge_id: str) -> str:
        """
        Re-adjudicate a challenged record under consensus and settle the stakes.

        The re-adjudication is a full, independent run of the same procedure that
        granted the access, over the holder's original evidence plus the challenger's
        new evidence, against the gate's current policy. Validators fetch and decide
        for themselves exactly as before.

        Upheld (the re-adjudication denies): access is revoked, the holder's deposit
        is slashed, the challenger recovers their stake plus half the deposit, and the
        gate treasury takes the other half.
        Rejected (the re-adjudication grants): access stands, the challenger's stake
        is slashed, half compensating the holder and half to the gate treasury.
        """
        challenge = self.challenges.get(challenge_id)
        self._require(
            challenge is not None, f"{ERROR_EXPECTED} Unknown challenge: {challenge_id}"
        )
        self._require(
            str(challenge.status) == CHALLENGE_OPEN,
            f"{ERROR_EXPECTED} Challenge is already {str(challenge.status)}",
        )

        gid = str(challenge.gate_id)
        gate = self._gate_or_raise(gid)
        now = self._now()
        key = self._access_key(gid, challenge.holder)

        record = self.access.get(key)
        self._require(
            record is not None,
            f"{ERROR_EXPECTED} Access record for this challenge no longer exists",
        )

        application = self.applications.get(str(record.application_id))
        self._require(
            application is not None,
            f"{ERROR_EXPECTED} Originating application is missing for this record",
        )

        original_urls = json.loads(str(application.evidence_urls_json))
        challenge_urls = json.loads(str(challenge.evidence_urls_json))
        combined = list(original_urls)
        for url in challenge_urls:
            if url not in combined:
                combined.append(url)

        spec = self._build_spec(gate, application)
        spec["evidence_urls"] = combined
        spec["applicant_note"] = (
            str(application.applicant_note)
            + " | CHALLENGE FILED: "
            + str(challenge.reason)
        )[: MAX_NOTE_TEXT * 2]

        result = _adjudicate_with_consensus(spec)
        decision = str(result.get("decision", DECISION_DENIED))
        if decision not in (DECISION_GRANTED, DECISION_DENIED):
            decision = DECISION_DENIED

        challenge.resolved_at = now
        challenge.outcome_decision = decision
        challenge.outcome_denial_code = str(result.get("denial_code", ""))[:64]
        challenge.outcome_reasoning = str(result.get("reasoning", ""))[:600]

        stake = int(challenge.stake_wei)
        deposit = int(record.deposit_wei)
        payout_challenger = 0
        payout_holder = 0
        treasury_credit = 0

        if decision == DECISION_DENIED:
            challenge.status = CHALLENGE_UPHELD
            challenger_share = deposit // 2
            treasury_credit = deposit - challenger_share
            payout_challenger = stake + challenger_share
            record.deposit_wei = u256(0)
            self._close_record(
                key,
                record,
                ACCESS_REVOKED,
                "Revoked by upheld challenge " + challenge_id,
                now,
            )
            gate.total_revoked = u32(int(gate.total_revoked) + 1)
            self.last_denied_at[key] = now
        else:
            challenge.status = CHALLENGE_REJECTED
            payout_holder = stake // 2
            treasury_credit = stake - payout_holder
            self.access[key] = record

        del self.open_challenge[key]
        self.challenges[challenge_id] = challenge

        if treasury_credit > 0:
            gate.treasury_wei = u256(int(gate.treasury_wei) + treasury_credit)
            self.total_treasury_wei = u256(
                int(self.total_treasury_wei) + treasury_credit
            )
        self.gates[gid] = gate

        released = payout_challenger + payout_holder + treasury_credit
        self.total_locked_wei = u256(int(self.total_locked_wei) - released)

        if payout_challenger > 0:
            gl.get_contract_at(challenge.challenger).emit_transfer(
                value=u256(payout_challenger), on="finalized"
            )
        if payout_holder > 0:
            gl.get_contract_at(challenge.holder).emit_transfer(
                value=u256(payout_holder), on="finalized"
            )

        return json.dumps(
            {
                "challenge_id": challenge_id,
                "gate_id": gid,
                "status": str(challenge.status),
                "decision": decision,
                "denial_code": str(challenge.outcome_denial_code),
                "challenger_payout_wei": str(payout_challenger),
                "holder_payout_wei": str(payout_holder),
                "treasury_credit_wei": str(treasury_credit),
                "access_status": str(record.status),
                "reasoning": str(challenge.outcome_reasoning),
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Composability surface
    # ------------------------------------------------------------------

    @gl.public.view
    def is_approved(self, gate_id: str, subject: Address) -> bool:
        """
        The one call consumer contracts make. Cheap, deterministic, no consensus.

        Returns True only when the subject holds an ACTIVE record that has not
        expired and that carries the gate's current policy version. Adjudication
        already happened; this is a cached lookup, so any contract can gate a method
        on a natural language policy for the cost of one cross-contract view.

        Integrators: pin (registry address, gate_id) and verify gate ownership once
        at deployment. The gate_id namespace is permissionless by design.
        """
        gate = self.gates.get(gate_id.strip().lower())
        if gate is None:
            return False
        record = self.access.get(self._access_key(str(gate.gate_id), subject))
        if record is None:
            return False
        return self._record_is_live(record, gate, self._now())

    @gl.public.view
    def access_status(self, gate_id: str, subject: Address) -> str:
        """
        The detailed form of is_approved, as JSON.

        Use this when a consumer needs to tell a user why they were turned away:
        no record at all, expired, revoked, or invalidated by a policy update.
        `reason` is a stable machine readable code.
        """
        gid = gate_id.strip().lower()
        subject = _as_address(subject)
        gate = self.gates.get(gid)
        if gate is None:
            return json.dumps(
                {"approved": False, "reason": "UNKNOWN_GATE", "gate_id": gid},
                sort_keys=True,
            )

        now = self._now()
        record = self.access.get(self._access_key(gid, subject))
        if record is None:
            return json.dumps(
                {
                    "approved": False,
                    "reason": "NO_RECORD",
                    "gate_id": gid,
                    "subject": subject.as_hex,
                    "current_policy_version": int(gate.policy_version),
                },
                sort_keys=True,
            )

        status = str(record.status)
        if status != ACCESS_ACTIVE:
            reason = (
                "REVOKED"
                if status == ACCESS_REVOKED
                else "RELEASED"
                if status == ACCESS_RELEASED
                else status
            )
        elif int(record.policy_version) != int(gate.policy_version):
            reason = "POLICY_SUPERSEDED"
        elif int(record.expires_at) <= int(now):
            reason = "EXPIRED"
        else:
            reason = "OK"

        return json.dumps(
            {
                "approved": reason == "OK",
                "reason": reason,
                "gate_id": gid,
                "subject": subject.as_hex,
                "record_status": status,
                "record_policy_version": int(record.policy_version),
                "current_policy_version": int(gate.policy_version),
                "granted_at": int(record.granted_at),
                "expires_at": int(record.expires_at),
                "seconds_remaining": max(0, int(record.expires_at) - int(now)),
                "application_id": str(record.application_id),
                "close_reason": str(record.close_reason),
                "challenge_count": int(record.challenge_count),
                "has_open_challenge": self.open_challenge.get(
                    self._access_key(gid, subject)
                )
                is not None,
            },
            sort_keys=True,
        )

    @gl.public.view
    def binding_token(self, gate_id: str, subject: Address) -> str:
        """
        The exact string the subject must publish on their evidence page.

        Front ends show this to the applicant before they apply. The token contains
        the applicant's own address, so it proves control of that address rather than
        merely pointing at a document about someone.
        """
        return _binding_token(gate_id.strip().lower(), _as_address(subject).as_hex)

    @gl.public.view
    def can_apply(self, gate_id: str, subject: Address) -> str:
        """
        Pre-flight check so a front end never sends a transaction that will revert.

        Returns JSON with an `eligible` flag, a stable `reason` code, and the bond
        the caller must attach.
        """
        gid = gate_id.strip().lower()
        subject = _as_address(subject)
        gate = self.gates.get(gid)
        if gate is None:
            return json.dumps(
                {"eligible": False, "reason": "UNKNOWN_GATE"}, sort_keys=True
            )

        now = self._now()
        key = self._access_key(gid, subject)
        reason = "OK"

        if bool(gate.paused):
            reason = "GATE_PAUSED"
        elif self.open_application.get(key) is not None:
            reason = "APPLICATION_PENDING"
        else:
            record = self.access.get(key)
            if record is not None and self._record_is_live(record, gate, now):
                reason = "ALREADY_APPROVED"
            else:
                cooldown = int(gate.reapply_cooldown_seconds)
                last_denied = self.last_denied_at.get(key)
                if (
                    cooldown > 0
                    and last_denied is not None
                    and int(now) < int(last_denied) + cooldown
                ):
                    reason = "COOLDOWN_ACTIVE"

        last_denied = self.last_denied_at.get(key)
        cooldown_until = 0
        if last_denied is not None and int(gate.reapply_cooldown_seconds) > 0:
            cooldown_until = int(last_denied) + int(gate.reapply_cooldown_seconds)

        return json.dumps(
            {
                "eligible": reason == "OK",
                "reason": reason,
                "gate_id": gid,
                "subject": subject.as_hex,
                "required_bond_wei": str(int(gate.bond_wei)),
                "policy_version": int(gate.policy_version),
                "binding_required": bool(gate.binding_required),
                "binding_token": _binding_token(gid, subject.as_hex),
                "cooldown_until": cooldown_until,
                "pending_application_id": self.open_application.get(key) or "",
            },
            sort_keys=True,
        )

    @gl.public.view
    def evidence_host_allowed(self, gate_id: str, url: str) -> bool:
        """Check a candidate evidence URL against the gate allowlist before applying."""
        gate = self.gates.get(gate_id.strip().lower())
        if gate is None:
            return False
        if not str(url).strip().lower().startswith("https://"):
            return False
        host = _host_of(url)
        if host == "":
            return False
        return _host_allowed(host, json.loads(str(gate.allowed_hosts_json)))

    # ------------------------------------------------------------------
    # Read views
    # ------------------------------------------------------------------

    @gl.public.view
    def get_gate(self, gate_id: str) -> str:
        """Full gate configuration and counters as JSON. Empty string if unknown."""
        gate = self.gates.get(gate_id.strip().lower())
        if gate is None:
            return ""
        return json.dumps(
            {
                "gate_id": str(gate.gate_id),
                "owner": gate.owner.as_hex,
                "title": str(gate.title),
                "policy_text": str(gate.policy_text),
                "conditions": json.loads(str(gate.conditions_json)),
                "disqualifiers": json.loads(str(gate.disqualifiers_json)),
                "allowed_hosts": json.loads(str(gate.allowed_hosts_json)),
                "fetch_mode": str(gate.fetch_mode),
                "binding_required": bool(gate.binding_required),
                "require_grounded_quotes": bool(gate.require_grounded_quotes),
                "policy_version": int(gate.policy_version),
                "access_ttl_seconds": int(gate.access_ttl_seconds),
                "bond_wei": str(int(gate.bond_wei)),
                "challenge_stake_wei": str(int(gate.challenge_stake_wei)),
                "reapply_cooldown_seconds": int(gate.reapply_cooldown_seconds),
                "paused": bool(gate.paused),
                "created_at": int(gate.created_at),
                "updated_at": int(gate.updated_at),
                "total_applications": int(gate.total_applications),
                "total_granted": int(gate.total_granted),
                "total_denied": int(gate.total_denied),
                "total_revoked": int(gate.total_revoked),
                "treasury_wei": str(int(gate.treasury_wei)),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_policy(self, gate_id: str) -> str:
        """
        The policy exactly as validators see it, plus its version.

        This is the object an applicant should read before spending a bond, and the
        object an auditor should diff after an update_policy call.
        """
        gate = self.gates.get(gate_id.strip().lower())
        if gate is None:
            return ""
        return json.dumps(
            {
                "gate_id": str(gate.gate_id),
                "policy_version": int(gate.policy_version),
                "policy_text": str(gate.policy_text),
                "conditions": json.loads(str(gate.conditions_json)),
                "disqualifiers": json.loads(str(gate.disqualifiers_json)),
                "updated_at": int(gate.updated_at),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_application(self, application_id: str) -> str:
        """The full application record including the adjudication audit trail."""
        application = self.applications.get(application_id)
        if application is None:
            return ""
        return json.dumps(
            {
                "application_id": str(application.application_id),
                "gate_id": str(application.gate_id),
                "applicant": application.applicant.as_hex,
                "evidence_urls": json.loads(str(application.evidence_urls_json)),
                "applicant_note": str(application.applicant_note),
                "bond_wei": str(int(application.bond_wei)),
                "policy_version": int(application.policy_version),
                "status": str(application.status),
                "created_at": int(application.created_at),
                "adjudicated_at": int(application.adjudicated_at),
                "decision": str(application.decision),
                "denial_code": str(application.denial_code),
                "failed_ids": json.loads(str(application.failed_ids_json)),
                "conditions": json.loads(str(application.conditions_json)),
                "disqualifiers": json.loads(str(application.disqualifiers_json)),
                "evidence_status": json.loads(str(application.evidence_status_json)),
                "reasoning": str(application.reasoning),
                "binding_ok": bool(application.binding_ok),
                "evidence_ok": bool(application.evidence_ok),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_access_record(self, gate_id: str, holder: Address) -> str:
        """The raw access record as stored. Empty string if none was ever issued."""
        record = self.access.get(self._access_key(gate_id.strip().lower(), holder))
        if record is None:
            return ""
        return json.dumps(
            {
                "gate_id": str(record.gate_id),
                "holder": record.holder.as_hex,
                "application_id": str(record.application_id),
                "policy_version": int(record.policy_version),
                "granted_at": int(record.granted_at),
                "expires_at": int(record.expires_at),
                "status": str(record.status),
                "deposit_wei": str(int(record.deposit_wei)),
                "closed_at": int(record.closed_at),
                "close_reason": str(record.close_reason),
                "challenge_count": int(record.challenge_count),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_challenge(self, challenge_id: str) -> str:
        """The challenge record and, once resolved, its outcome."""
        challenge = self.challenges.get(challenge_id)
        if challenge is None:
            return ""
        return json.dumps(
            {
                "challenge_id": str(challenge.challenge_id),
                "gate_id": str(challenge.gate_id),
                "holder": challenge.holder.as_hex,
                "challenger": challenge.challenger.as_hex,
                "stake_wei": str(int(challenge.stake_wei)),
                "reason": str(challenge.reason),
                "evidence_urls": json.loads(str(challenge.evidence_urls_json)),
                "status": str(challenge.status),
                "created_at": int(challenge.created_at),
                "resolved_at": int(challenge.resolved_at),
                "outcome_decision": str(challenge.outcome_decision),
                "outcome_denial_code": str(challenge.outcome_denial_code),
                "outcome_reasoning": str(challenge.outcome_reasoning),
            },
            sort_keys=True,
        )

    @gl.public.view
    def list_gates(self, offset: int, limit: int) -> str:
        """Paginated list of gate ids in registration order."""
        total = int(self.gate_count)
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        ids = []
        index = start
        while index < total and len(ids) < count:
            gid = self.gate_index.get(u32(index))
            if gid is not None:
                ids.append(str(gid))
            index += 1
        return json.dumps(
            {"total": total, "offset": start, "gate_ids": ids}, sort_keys=True
        )

    @gl.public.view
    def list_applications(self, gate_id: str, offset: int, limit: int) -> str:
        """Paginated list of application ids filed against one gate."""
        gid = gate_id.strip().lower()
        total = int(self.gate_application_count.get(gid) or 0)
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        ids = []
        index = start
        while index < total and len(ids) < count:
            value = self.gate_applications.get(gid + KEY_SEP + str(index))
            if value is not None:
                ids.append(str(value))
            index += 1
        return json.dumps(
            {"gate_id": gid, "total": total, "offset": start, "application_ids": ids},
            sort_keys=True,
        )

    @gl.public.view
    def list_holders(self, gate_id: str, offset: int, limit: int) -> str:
        """
        Paginated list of every address ever granted access to a gate, with the live
        status of each. `live` is the same predicate is_approved evaluates.
        """
        gid = gate_id.strip().lower()
        gate = self.gates.get(gid)
        if gate is None:
            return ""
        now = self._now()
        total = int(self.gate_holder_count.get(gid) or 0)
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        holders = []
        index = start
        while index < total and len(holders) < count:
            raw = self.gate_holders.get(gid + KEY_SEP + str(index))
            if raw is not None:
                key = gid + KEY_SEP + str(raw).lower()
                record = self.access.get(key)
                if record is not None:
                    holders.append(
                        {
                            "holder": str(raw),
                            "status": str(record.status),
                            "policy_version": int(record.policy_version),
                            "expires_at": int(record.expires_at),
                            "live": self._record_is_live(record, gate, now),
                        }
                    )
            index += 1
        return json.dumps(
            {
                "gate_id": gid,
                "total": total,
                "offset": start,
                "current_policy_version": int(gate.policy_version),
                "holders": holders,
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_applicant_applications(
        self, applicant: Address, offset: int, limit: int
    ) -> str:
        """Paginated list of every application filed by one address across all gates."""
        applicant = _as_address(applicant)
        total = int(self.applicant_application_count.get(applicant) or 0)
        start = max(0, int(offset))
        count = max(0, min(int(limit), 100))
        ids = []
        index = start
        prefix = applicant.as_hex.lower() + KEY_SEP
        while index < total and len(ids) < count:
            value = self.applicant_applications.get(prefix + str(index))
            if value is not None:
                ids.append(str(value))
            index += 1
        return json.dumps(
            {
                "applicant": applicant.as_hex,
                "total": total,
                "offset": start,
                "application_ids": ids,
            },
            sort_keys=True,
        )

    @gl.public.view
    def gate_stats(self, gate_id: str) -> str:
        """Counters and live holder tally for one gate."""
        gid = gate_id.strip().lower()
        gate = self.gates.get(gid)
        if gate is None:
            return ""
        now = self._now()
        total_holders = int(self.gate_holder_count.get(gid) or 0)
        live = 0
        index = 0
        while index < total_holders:
            raw = self.gate_holders.get(gid + KEY_SEP + str(index))
            if raw is not None:
                record = self.access.get(gid + KEY_SEP + str(raw).lower())
                if record is not None and self._record_is_live(record, gate, now):
                    live += 1
            index += 1
        return json.dumps(
            {
                "gate_id": gid,
                "owner": gate.owner.as_hex,
                "policy_version": int(gate.policy_version),
                "paused": bool(gate.paused),
                "total_applications": int(gate.total_applications),
                "total_granted": int(gate.total_granted),
                "total_denied": int(gate.total_denied),
                "total_revoked": int(gate.total_revoked),
                "ever_granted_holders": total_holders,
                "live_holders": live,
                "treasury_wei": str(int(gate.treasury_wei)),
            },
            sort_keys=True,
        )

    @gl.public.view
    def get_registry_stats(self) -> str:
        """
        Registry wide totals, including the solvency figures.

        locked_wei is bonds on pending applications plus deposits behind live records
        plus open challenge stakes. treasury_wei is what gate owners may withdraw.
        The contract balance must always be at least locked_wei plus treasury_wei.
        """
        return json.dumps(
            {
                "registry_owner": self.registry_owner.as_hex,
                "gate_count": int(self.gate_count),
                "application_count": int(self.application_count),
                "challenge_count": int(self.challenge_count),
                "locked_wei": str(int(self.total_locked_wei)),
                "treasury_wei": str(int(self.total_treasury_wei)),
            },
            sort_keys=True,
        )
