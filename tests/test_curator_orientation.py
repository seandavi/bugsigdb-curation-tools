"""Group-orientation convention: S4 defines it, S5b / NER are told which group is which.

BugSigDB reports taxa INCREASED/DECREASED in Group 1 relative to Group 0, with Group 0 the
reference/control group (~95% of curated experiments where a control is identifiable). Before this,
S4 never stated the convention and S5b/NER were asked for direction "in Group 1" without being told
the groups -- directions flipped wholesale.
"""

from __future__ import annotations

import asyncio

import httpx
import test_curator_pipeline_e2e as e2e

from bugsigdb_curation.curator.artifact_text import group_orientation_text
from bugsigdb_curation.curator.evidence import EvidenceBundle, EvidenceTable
from bugsigdb_curation.curator.experiment import build_experiment_messages
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.ner import build_ner_messages
from bugsigdb_curation.curator.pipeline import curate_async
from bugsigdb_curation.curator.segment import ExperimentStub
from bugsigdb_curation.curator.signature import build_signature_messages
from bugsigdb_curation.retrieval import ArticleMetadata, SectionEntry

_TABLE = EvidenceTable(table_id="T1", number="1", label="Table 1.", caption="LEfSe taxa", rows=(("Taxon", "LDA"), ("A", "4")))
_ARTIFACT = LocatedArtifact(kind="table", table=_TABLE)


def _text(messages) -> str:
    return messages[0]["content"][0]["text"]


def test_orientation_text_names_both_groups_and_the_direction_rule():
    text = group_orientation_text("HC", "ATB")
    assert "Group 0" in text and "HC" in text and "Group 1" in text and "ATB" in text
    assert "INCREASED means more abundant in Group 1 than in Group 0" in text
    assert "never assume the left/top/first-listed group is Group 1" in text


def test_orientation_text_is_empty_unless_both_names_are_known():
    for pair in ((None, "x"), ("x", None), ("", "x"), ("x", "  "), (None, None)):
        assert group_orientation_text(*pair) == ""


def test_signature_prompt_includes_groups_when_known_and_is_unchanged_otherwise():
    with_groups = _text(build_signature_messages(_ARTIFACT, groups=("Healthy controls", "Active TB")))
    assert "Healthy controls" in with_groups and "Active TB" in with_groups and "Group 0" in with_groups
    plain = _text(build_signature_messages(_ARTIFACT))
    assert "Group 0 (the reference" not in plain
    assert _text(build_signature_messages(_ARTIFACT, groups=(None, "x"))) == plain


def test_ner_prompt_includes_groups_when_known():
    assert "Active TB" in _text(build_ner_messages(_ARTIFACT, groups=("Healthy controls", "Active TB")))
    assert "Group 0 (the reference" not in _text(build_ner_messages(_ARTIFACT))


def test_s4_prompt_states_the_convention():
    bundle = EvidenceBundle(
        pmid="1",
        pmcid="PMC1",
        metadata=ArticleMetadata(title="t", journal="j", year=2026, authors=("a",), doi=None),
        sections=(SectionEntry(section_id="s", title="Methods", text="ATB patients vs healthy controls (HC)"),),
        tables=(),
        figures=(),
    )
    text = _text(build_experiment_messages(bundle, ExperimentStub(index=0, description="ATB vs HC")))
    assert "group_0 is the REFERENCE group" in text and "group_1 is the CASE group" in text
    assert "'ATB vs HC' -> group_0 = HC, group_1 = ATB" in text


def test_pipeline_threads_s4_group_names_into_the_signature_prompt(httpx_mock, tmp_path):
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)
    model = MockModel(
        responses={
            "experiment_metadata": {
                "host_species": "Homo sapiens",
                "body_site": ["Feces"],
                "condition": ["Disease"],
                "group_0_name": "Healthy controls",
                "group_1_name": "Active TB",
                "sequencing_type": "16S",
                "statistical_test": [],
                "mht_correction": None,
            }
        }
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(e2e.PMID, model=model, client=client, taxonomy_cache_path=tmp_path / "c.json")

    asyncio.run(run())
    (call,) = [c for c in model.calls if c["stage"] == "signature_extract"]
    prompt = call["messages"][0]["content"][0]["text"]
    assert "Healthy controls" in prompt and "Active TB" in prompt and "Group 1 (the case" in prompt
