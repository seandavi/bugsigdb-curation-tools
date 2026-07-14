"""Unit tests for `bugsigdb_curation.curator.ner` (S5b-NER, split A1's names-only extraction)."""

from __future__ import annotations

from bugsigdb_curation.curator.evidence import EvidenceFigure, EvidenceTable
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.ner import NamedTaxon, build_ner_messages, extract_names

_TABLE = EvidenceTable(
    table_id="T2", number="2", label="Table 2.", caption="DA taxa.", rows=(("Taxon", "Direction"), ("Bacteroides", "up"))
)
_FIGURE = EvidenceFigure(
    figure_id="F1", number="1", label="Figure 1.", legend="LEfSe cladogram.",
    graphic_filename="f1.jpg", blob_url="https://cdn/f1.jpg",
)


def test_build_ner_messages_table_is_text_only_and_never_mentions_ids():
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    messages = build_ner_messages(artifact)
    content = messages[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "Table 2" in content[0]["text"]
    assert "Bacteroides" in content[0]["text"]
    assert "proposed_ncbi_id" not in content[0]["text"]


def test_build_ner_messages_figure_includes_image_when_bytes_given():
    artifact = LocatedArtifact(kind="figure", figure=_FIGURE)
    messages = build_ner_messages(artifact, image_bytes=b"fake-png-bytes")
    content = messages[0]["content"]
    assert len(content) == 2
    assert content[1]["type"] == "image_url"


def test_extract_names_parses_names_and_directions_no_ids():
    model = MockModel(
        responses={
            "signature_ner": {
                "taxa": [
                    {"name": "Bacteroides fragilis", "direction": "increased"},
                    {"name": "Faecalibacterium prausnitzii", "direction": "decreased"},
                ]
            }
        }
    )
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    names = extract_names(artifact, model=model)

    assert {n.name for n in names} == {"Bacteroides fragilis", "Faecalibacterium prausnitzii"}
    assert {n.direction for n in names} == {"increased", "decreased"}


def test_extract_names_skips_malformed_items():
    model = MockModel(
        responses={
            "signature_ner": {
                "taxa": [
                    {"name": "", "direction": "increased"},
                    {"name": "X", "direction": "sideways"},
                    "not-a-dict",
                    {"name": "Bacteroides fragilis", "direction": "increased"},
                ]
            }
        }
    )
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    names = extract_names(artifact, model=model)
    assert len(names) == 1
    assert names[0].name == "Bacteroides fragilis"


def test_extract_names_normalizes_direction_case_and_whitespace():
    model = MockModel(responses={"signature_ner": {"taxa": [{"name": "X", "direction": " Increased "}]}})
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    names = extract_names(artifact, model=model)
    assert names[0].direction == "increased"


def test_extract_names_uses_custom_stage_for_reviewer_reuse():
    """`curator.panel` reuses this function with `stage="review_signature"` --
    the call must actually route through that stage name in `MockModel`."""
    model = MockModel(responses={"review_signature": {"taxa": [{"name": "Y", "direction": "decreased"}]}})
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    names = extract_names(artifact, model=model, stage="review_signature")
    assert names == [NamedTaxon(name="Y", direction="decreased")]
    assert model.calls[0]["stage"] == "review_signature"
