"""Per-experiment artifact search: S5a hands S5b several ranked candidates, S5b may decline one.

Observed failure (PMID 42404767): one top-ranked artifact was extracted for EVERY experiment stub, so a
paper with several comparisons got the same taxa copied into each experiment.
"""

from __future__ import annotations

import test_curator_routing as routing_helpers

from bugsigdb_curation.curator.artifact_text import group_orientation_text
from bugsigdb_curation.curator.locate import LocatedArtifact, locate_artifact, locate_artifacts
from bugsigdb_curation.curator.ner import build_ner_messages
from bugsigdb_curation.curator.signature import build_signature_messages

_table = routing_helpers._table
_figure = routing_helpers._figure
_bundle = routing_helpers._bundle

_ESCAPE_HATCH = "does NOT report a comparison between these two groups"


def _ranked(*p_das: float) -> list[LocatedArtifact]:
    """Figures 1..n with the given p_da, already best-first."""
    return [LocatedArtifact(kind="figure", figure=_figure(str(i), "x"), p_da=p) for i, p in enumerate(p_das, 1)]


# --- locate_artifacts --------------------------------------------------------------------------


def test_locate_artifacts_ranked_keeps_those_at_or_above_min_p_best_first():
    ranked = _ranked(0.98, 0.97, 0.5, 0.49)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked, max_n=5)] == ["Figure 1", "Figure 2", "Figure 3"]


def test_locate_artifacts_ranked_caps_at_max_n():
    ranked = _ranked(0.99, 0.98, 0.97, 0.96)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked)] == ["Figure 1", "Figure 2", "Figure 3"]
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked, max_n=2)] == ["Figure 1", "Figure 2"]


def test_locate_artifacts_ranked_always_returns_the_top_one_even_below_min_p():
    ranked = _ranked(0.2, 0.1)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked)] == ["Figure 1"]
    assert locate_artifacts(_bundle(), ranked)[0] == locate_artifact(_bundle(), ranked)


def test_locate_artifacts_min_p_is_a_parameter():
    assert len(locate_artifacts(_bundle(), _ranked(0.9, 0.6, 0.3), min_p=0.25)) == 3


def test_locate_artifacts_unranked_is_the_regex_choice_alone():
    bundle = _bundle([_table("1", "LEfSe summary"), _table("2", "differential taxa")], [_figure("1", "LEfSe")])
    assert locate_artifacts(bundle) == [locate_artifact(bundle)]
    assert locate_artifacts(bundle, []) == [locate_artifact(bundle)]


def test_locate_artifacts_with_nothing_to_locate_is_empty():
    assert locate_artifacts(_bundle()) == []


# --- the escape hatch in the S5b / NER prompts -------------------------------------------------


def _text(messages) -> str:
    return messages[0]["content"][0]["text"]


_ARTIFACT = LocatedArtifact(kind="table", table=_table("1", "LEfSe taxa"))


def test_orientation_text_has_the_escape_hatch_when_names_are_known():
    text = group_orientation_text("HC", "ATB")
    assert _ESCAPE_HATCH in text
    assert '{"taxa": []}' in text
    assert "do not fill in taxa from a different comparison" in text


def test_orientation_text_has_no_escape_hatch_without_names():
    assert group_orientation_text(None, "ATB") == ""


def test_signature_and_ner_prompts_carry_the_escape_hatch_iff_names_are_known():
    groups = ("Healthy controls", "Active TB")
    for build in (build_signature_messages, build_ner_messages):
        assert _ESCAPE_HATCH in _text(build(_ARTIFACT, groups=groups))
        assert _ESCAPE_HATCH not in _text(build(_ARTIFACT))
        assert _ESCAPE_HATCH not in _text(build(_ARTIFACT, groups=(None, "x")))
