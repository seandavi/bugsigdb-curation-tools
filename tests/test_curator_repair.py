"""Unit tests for `bugsigdb_curation.curator.repair` (the bounded direction
repair loop shared by split-verify's verifier and split-panel's reviewer)."""

from __future__ import annotations

from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.repair import build_direction_rederive_messages, resolve_direction_with_repair

STAGE = "verify_direction"


def test_round_one_confirms_the_claim_no_repair_needed():
    model = MockModel(responses={STAGE: {"direction": "decreased"}})
    final, changed = resolve_direction_with_repair("X", "decreased", "source text", model=model, stage=STAGE)
    assert final == "decreased"
    assert changed is False
    assert len(model.calls) == 1


def test_disagreement_stabilizes_on_round_two_and_flips():
    """Round 1 disagrees with the claim; round 2's independent re-derivation
    (same fixed response) agrees with round 1 -- the repair is accepted and
    the direction flips to the re-derived value."""
    model = MockModel(responses={STAGE: {"direction": "increased"}})
    final, changed = resolve_direction_with_repair("X", "decreased", "source text", model=model, stage=STAGE)
    assert final == "increased"
    assert changed is True
    assert len(model.calls) == 2


def test_persistent_non_convergence_exhausts_the_cap_and_is_unresolved():
    """A model that never returns a parseable direction (e.g. persistently
    malformed output) can never satisfy either termination condition
    (agreeing with the claim, or two consecutive rounds agreeing with each
    other) -- the loop must still terminate at exactly `max_rounds` calls
    (no infinite loop) rather than spinning forever, and report unresolved
    rather than shipping a guess."""
    model = MockModel(responses={STAGE: {"direction": "sideways"}})  # never a valid increased/decreased
    final, changed = resolve_direction_with_repair(
        "X", "decreased", "source text", model=model, stage=STAGE, max_rounds=5
    )
    assert final is None
    assert changed is True
    assert len(model.calls) == 5  # capped at exactly max_rounds, never more


def test_round_one_disagrees_then_round_two_reagrees_with_claim_stays_unresolved():
    """Round 1 disagrees with the claim ("increased" vs claimed "decreased");
    round 2 reproduces the ORIGINAL claim ("decreased") but does NOT agree
    with round 1's own derivation ("increased"). Per the docstring, only
    round 1 is ever compared directly against `claimed_direction` -- every
    later round is compared only against the round immediately before it.
    Round 2 reproducing the claim in isolation (without round 1 also
    agreeing) is therefore NOT itself a confirmation: the two-consecutive-
    round rule isn't satisfied (round 1 = "increased" != round 2 =
    "decreased"), so with `max_rounds=2` the loop exhausts unresolved.

    (The pre-fix code instead re-checked every round against the original
    claim unconditionally, so it would have returned `("decreased", False)`
    at round 2 -- a false "confirmed, no repair" outcome. This asserts the
    documented behavior instead.)
    """
    call_count = {"n": 0}

    def per_round(_messages: list[dict]) -> dict:
        call_count["n"] += 1
        return {"direction": "increased" if call_count["n"] == 1 else "decreased"}

    model = MockModel(responses={STAGE: per_round})

    final, changed = resolve_direction_with_repair(
        "X", "decreased", "source text", model=model, stage=STAGE, max_rounds=2
    )

    assert final is None
    assert changed is True
    assert len(model.calls) == 2


def test_max_rounds_is_the_hard_cap_on_model_calls():
    model = MockModel(responses={STAGE: {"direction": "increased"}})
    resolve_direction_with_repair("X", "decreased", "source text", model=model, stage=STAGE, max_rounds=2)
    assert len(model.calls) == 2  # round 1 disagrees, round 2 stabilizes -- never a 3rd call


def test_unparseable_direction_is_tolerated_and_does_not_crash():
    model = MockModel(responses={STAGE: {"direction": "sideways"}})
    final, changed = resolve_direction_with_repair("X", "decreased", "source text", model=model, stage=STAGE, max_rounds=2)
    assert final is None
    assert changed is True


def test_build_direction_rederive_messages_includes_image_when_bytes_given():
    messages = build_direction_rederive_messages("X", "source text", round_num=1, image_bytes=b"x")
    content = messages[0]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_build_direction_rederive_messages_omits_image_when_bytes_none():
    messages = build_direction_rederive_messages("X", "source text", round_num=1, image_bytes=None)
    content = messages[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"


def test_resolve_direction_with_repair_forwards_image_bytes_to_every_round():
    model = MockModel(responses={STAGE: {"direction": "increased"}})
    resolve_direction_with_repair(
        "X", "decreased", "source text", model=model, stage=STAGE, max_rounds=2, image_bytes=b"fake-png-bytes"
    )
    assert len(model.calls) == 2
    for call in model.calls:
        content = call["messages"][0]["content"]
        assert len(content) == 2
        assert content[1]["type"] == "image_url"


def test_prompt_never_reveals_the_claimed_direction():
    """The re-derivation prompt must show only the taxon name + source text
    -- never the extractor's claim, per "sees only the claim + the cited
    source evidence, NOT the extractor's reasoning" (the claim compared here
    means the *taxon claim*, i.e. which taxon+source is under review -- the
    claimed *direction* itself must stay out of this prompt so the
    re-derivation is genuinely independent)."""
    model = MockModel(responses={STAGE: {"direction": "increased"}})
    resolve_direction_with_repair("Some Taxon", "decreased", "the source text goes here", model=model, stage=STAGE)
    prompt_text = model.calls[0]["messages"][0]["content"][0]["text"]
    assert "Some Taxon" in prompt_text
    assert "the source text goes here" in prompt_text
    # "decreased" (the claim) must not leak into the prompt itself.
    assert "claimed" not in prompt_text.lower()
