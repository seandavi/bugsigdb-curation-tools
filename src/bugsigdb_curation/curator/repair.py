"""Shared bounded-repair-loop helper for A2's two verified designs.

Both `curator.verify` (split-verify's adversarial verifier) and
`curator.panel` (split-panel's reviewer arbitration) need to independently
re-derive a taxon's `abundance_in_group_1` direction from the cited source
and reconcile it against a prior claim -- capped at a small, fixed number of
rounds so a persistent disagreement can never loop forever (workflow plan
§6b: "a bounded repair loop (hard cap, e.g. <=2 rounds); on exhaustion, mark
the field/taxon unresolved/flagged rather than shipping a guess"). Factored
here once rather than duplicated in both modules.
"""

from __future__ import annotations

from typing import Literal

from bugsigdb_curation.curator.model import Model, build_text_content

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")

#: Hard cap on repair rounds (workflow plan §6b: "e.g. <=2 rounds"). Both
#: `curator.verify` and `curator.panel` default to this.
DEFAULT_MAX_REPAIR_ROUNDS = 2


def _parse_direction(value: object) -> Direction | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized if normalized in _DIRECTIONS else None


def build_direction_rederive_messages(taxon_name: str, source_text: str, *, round_num: int) -> list[dict]:
    """A fresh-context re-derivation prompt: shows only the taxon name + the
    cited source text -- never the prior claim or any extractor reasoning,
    per the plan's "sees only the claim + the cited source evidence, NOT the
    extractor's reasoning". `round_num` is informational only (no branching
    on it here); a caller running a second round calls this again for an
    independent second opinion, not a continuation of the first."""
    prompt = (
        f"A microbiome research paper reports the taxon {taxon_name!r} as differentially "
        "abundant between two compared groups (Group 0 = control/reference, Group 1 = "
        "case/exposed). Based ONLY on the source text below, is this taxon INCREASED or "
        "DECREASED in Group 1 relative to Group 0?\n\n"
        f"Source:\n{source_text}\n\n"
        'Return ONLY a JSON object: {"direction": "increased"|"decreased"}'
    )
    return [{"role": "user", "content": [build_text_content(prompt)]}]


def resolve_direction_with_repair(
    taxon_name: str,
    claimed_direction: Direction,
    source_text: str,
    *,
    model: Model,
    stage: str,
    max_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
) -> tuple[Direction | None, bool]:
    """Independently re-derive `taxon_name`'s direction and reconcile it
    against `claimed_direction`, capped at `max_rounds` model calls.

    Round 1 re-derives the direction from `source_text` alone; if it agrees
    with `claimed_direction`, that's confirmed -- returns `(claimed_direction,
    False)`, no repair needed. This round-1-vs-claim comparison happens
    exactly once, on round 1 only. On disagreement, each further round
    re-derives again (same fresh-context prompt); a repair is accepted once
    two consecutive rounds' derivations *agree with each other* (the
    independent signal has stabilized on one answer) -- every round after
    round 1 is compared only against the immediately preceding round's
    derivation, never re-checked against the original claim on its own. If
    the value two consecutive rounds stabilize on happens to equal
    `claimed_direction`, that is reported as a clean, unchanged confirmation
    (`changed=False`) since nothing about the record actually flips;
    stabilizing on any other value is an accepted repair -- returns
    `(new_direction, True)`. A later round reproducing `claimed_direction`
    in isolation (without the round right before it also agreeing) is *not*
    itself a confirmation -- it must still satisfy the two-consecutive-round
    rule like any other candidate value.

    If `max_rounds` is exhausted with no two consecutive rounds agreeing
    (including the model returning unparseable output every round), the
    field is unresolved: returns `(None, True)`. The caller must not ship a
    guess for this taxon -- flag it and drop it from the assembled record.

    `changed` (the second tuple element) is True iff either a repair
    actually flipped the direction, or the field was left unresolved --
    i.e. False only for a "confirmed, nothing to repair" outcome (either
    round 1 outright, or a later two-consecutive-round stabilization that
    lands back on `claimed_direction`).
    """
    previous: Direction | None = None
    for round_num in range(1, max_rounds + 1):
        response = model.complete(
            stage=stage, messages=build_direction_rederive_messages(taxon_name, source_text, round_num=round_num)
        )
        derived = _parse_direction(response.get("direction"))
        if derived is None:
            continue
        if round_num == 1:
            if derived == claimed_direction:
                return derived, False
            previous = derived
            continue
        if derived == previous:
            return derived, derived != claimed_direction
        previous = derived
    return None, True
