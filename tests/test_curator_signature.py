"""Unit tests for `bugsigdb_curation.curator.signature` (S5b fused extract + S6 verify)."""

from __future__ import annotations

import asyncio

import httpx

from bugsigdb_curation.curator.evidence import EvidenceFigure, EvidenceTable
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.signature import build_signature_messages, extract_signatures
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver

_TABLE = EvidenceTable(
    table_id="T2", number="2", label="Table 2.", caption="DA taxa.", rows=(("Taxon", "Direction"), ("Bacteroides", "up"))
)
_FIGURE = EvidenceFigure(
    figure_id="F1", number="1", label="Figure 1.", legend="LEfSe cladogram.",
    graphic_filename="f1.jpg", blob_url="https://cdn/f1.jpg",
)


def _run(coro):
    return asyncio.run(coro)


# --- build_signature_messages ----------------------------------------------------------------


def test_build_signature_messages_table_is_text_only():
    artifact = LocatedArtifact(kind="table", table=_TABLE)
    messages = build_signature_messages(artifact)
    content = messages[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert "Table 2" in content[0]["text"]
    assert "Bacteroides" in content[0]["text"]


def test_build_signature_messages_figure_includes_image_when_bytes_given():
    artifact = LocatedArtifact(kind="figure", figure=_FIGURE)
    messages = build_signature_messages(artifact, image_bytes=b"fake-png-bytes")
    content = messages[0]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert "LEfSe cladogram" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_build_signature_messages_figure_without_bytes_is_text_only():
    artifact = LocatedArtifact(kind="figure", figure=_FIGURE)
    messages = build_signature_messages(artifact, image_bytes=None)
    assert len(messages[0]["content"]) == 1


# --- extract_signatures (fused extract + S6 verify) --------------------------------------------


def test_extract_signatures_groups_by_direction_and_verifies_ids(httpx_mock):
    model = MockModel(
        responses={
            "signature_extract": {
                "taxa": [
                    {"name": "Bacteroides fragilis", "direction": "increased", "proposed_ncbi_id": 817},
                    {"name": "Faecalibacterium prausnitzii", "direction": "decreased", "proposed_ncbi_id": 853},
                ]
            }
        }
    )
    resolver = NcbiTaxonomyResolver(
        cache={"bacteroides fragilis": 817, "faecalibacterium prausnitzii": 853}, cache_path=None
    )
    artifact = LocatedArtifact(kind="table", table=_TABLE)

    async def run():
        async with httpx.AsyncClient() as client:
            return await extract_signatures(artifact, model=model, resolver=resolver, client=client)

    signatures = _run(run())

    assert {s.direction for s in signatures} == {"increased", "decreased"}
    increased = next(s for s in signatures if s.direction == "increased")
    assert increased.taxa[0].taxon_name == "Bacteroides fragilis"
    assert increased.taxa[0].ncbi_id == 817


def test_extract_signatures_drops_unverified_proposed_id_but_keeps_the_taxon(httpx_mock):
    """A hallucinated/wrong id must never survive S6 -- but the taxon NAME is
    still kept, per "never guess" (drop the id, not the whole taxon)."""
    model = MockModel(
        responses={
            "signature_extract": {
                "taxa": [{"name": "Bacteroides fragilis", "direction": "increased", "proposed_ncbi_id": 999999}],
            }
        }
    )
    # The authority resolves this name to a DIFFERENT id than what was proposed.
    resolver = NcbiTaxonomyResolver(cache={"bacteroides fragilis": 817}, cache_path=None)
    artifact = LocatedArtifact(kind="table", table=_TABLE)

    async def run():
        async with httpx.AsyncClient() as client:
            return await extract_signatures(artifact, model=model, resolver=resolver, client=client)

    signatures = _run(run())

    assert len(signatures) == 1
    taxon = signatures[0].taxa[0]
    assert taxon.taxon_name == "Bacteroides fragilis"
    assert taxon.ncbi_id is None  # rejected, never guessed


def test_extract_signatures_keeps_taxon_unresolved_when_no_id_proposed(httpx_mock):
    model = MockModel(
        responses={
            "signature_extract": {
                "taxa": [{"name": "Some Novel Taxon", "direction": "increased", "proposed_ncbi_id": None}]
            }
        }
    )
    resolver = NcbiTaxonomyResolver(cache={"some novel taxon": None}, cache_path=None)
    artifact = LocatedArtifact(kind="table", table=_TABLE)

    async def run():
        async with httpx.AsyncClient() as client:
            return await extract_signatures(artifact, model=model, resolver=resolver, client=client)

    signatures = _run(run())
    assert signatures[0].taxa[0].ncbi_id is None


def test_extract_signatures_skips_malformed_items():
    model = MockModel(
        responses={
            "signature_extract": {
                "taxa": [
                    {"name": "", "direction": "increased"},  # empty name
                    {"name": "X", "direction": "sideways"},  # bad direction
                    "not-a-dict",
                    {"name": "Bacteroides fragilis", "direction": "increased", "proposed_ncbi_id": None},
                ]
            }
        }
    )
    resolver = NcbiTaxonomyResolver(cache={"bacteroides fragilis": 817}, cache_path=None)
    artifact = LocatedArtifact(kind="table", table=_TABLE)

    async def run():
        async with httpx.AsyncClient() as client:
            return await extract_signatures(artifact, model=model, resolver=resolver, client=client)

    signatures = _run(run())
    assert len(signatures) == 1
    assert len(signatures[0].taxa) == 1
    assert signatures[0].taxa[0].taxon_name == "Bacteroides fragilis"


def test_extract_signatures_uses_multimodal_message_for_figure_artifact():
    model = MockModel(responses={"signature_extract": {"taxa": []}})
    resolver = NcbiTaxonomyResolver(cache_path=None)
    artifact = LocatedArtifact(kind="figure", figure=_FIGURE)

    async def run():
        async with httpx.AsyncClient() as client:
            return await extract_signatures(artifact, model=model, resolver=resolver, client=client, image_bytes=b"img")

    _run(run())

    sent = model.calls[0]["messages"][0]["content"]
    assert sent[1]["type"] == "image_url"
