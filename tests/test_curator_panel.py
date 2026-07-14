"""Unit tests for `bugsigdb_curation.curator.panel` (split-panel's S10
independent reviewer: arbitration between extractor/reviewer + recall path)."""

from __future__ import annotations

import asyncio

import httpx

from bugsigdb_curation.curator.evidence import EvidenceTable
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.panel import review_signatures
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver

_TABLE = EvidenceTable(
    table_id="T2", number="2", label="Table 2.", caption="DA taxa.",
    rows=(("Taxon", "Direction"), ("Faecalibacterium prausnitzii", "decreased"), ("Escherichia coli", "increased")),
)
_ARTIFACT = LocatedArtifact(kind="table", table=_TABLE)


def _sig(direction, taxa):
    return ExtractedSignature(
        direction=direction, taxa=tuple(ExtractedTaxon(taxon_name=n, direction=direction, ncbi_id=i) for n, i in taxa)
    )


def _run(coro):
    return asyncio.run(coro)


def _review(signatures, model, resolver=None, max_repair_rounds=2):
    resolver = resolver or NcbiTaxonomyResolver(cache={}, cache_path=None, db=None)

    async def run():
        async with httpx.AsyncClient() as client:
            return await review_signatures(
                signatures,
                artifact=_ARTIFACT,
                model=model,
                resolver=resolver,
                client=client,
                source_context="",
                max_repair_rounds=max_repair_rounds,
            )

    return _run(run())


def test_agreed_taxon_is_accepted_as_is():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={"review_signature": {"taxa": [{"name": "Faecalibacterium prausnitzii", "direction": "decreased"}]}}
    )

    out, flags = _review(signatures, model)

    assert len(out) == 1
    assert out[0].direction == "decreased"
    assert out[0].taxa[0].ncbi_id == 853
    assert flags == ()


def test_disagreement_is_reconciled_via_bounded_repair():
    signatures = [_sig("increased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {"taxa": [{"name": "Faecalibacterium prausnitzii", "direction": "decreased"}]},
            "review_reconcile_direction": {"direction": "decreased"},
        }
    )

    out, flags = _review(signatures, model)

    assert len(out) == 1
    assert out[0].direction == "decreased"  # reconciled to the stable re-derivation
    assert out[0].taxa[0].ncbi_id == 853
    assert flags == ()


def test_irreconcilable_disagreement_is_flagged_and_dropped():
    signatures = [_sig("increased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {"taxa": [{"name": "Faecalibacterium prausnitzii", "direction": "decreased"}]},
            "review_reconcile_direction": {"direction": "sideways"},  # never parseable -> never converges
        }
    )

    out, flags = _review(signatures, model, max_repair_rounds=2)

    assert out == []
    assert any("unresolved" in f for f in flags)


def test_reviewer_only_taxon_is_added_when_grounded():
    """Recall path: the reviewer surfaces a taxon the extractor missed
    entirely -- it's added, with its id resolved via the authority (never a
    model-proposed id, same as every other split-A1 taxon)."""
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {
                "taxa": [
                    {"name": "Faecalibacterium prausnitzii", "direction": "decreased"},
                    {"name": "Escherichia coli", "direction": "increased"},
                ]
            },
            "review_ground_check": {"results": [{"name": "Escherichia coli", "in_source": True}]},
        }
    )
    resolver = NcbiTaxonomyResolver(cache={"escherichia coli": 562}, cache_path=None, db=None)

    out, flags = _review(signatures, model, resolver=resolver)

    all_names = {t.taxon_name for sig in out for t in sig.taxa}
    assert all_names == {"Faecalibacterium prausnitzii", "Escherichia coli"}
    added = next(t for sig in out for t in sig.taxa if t.taxon_name == "Escherichia coli")
    assert added.ncbi_id == 562
    assert added.direction == "increased"
    assert flags == ()


def test_reviewer_only_taxon_dropped_when_not_grounded():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {
                "taxa": [
                    {"name": "Faecalibacterium prausnitzii", "direction": "decreased"},
                    {"name": "Fabricated Bug", "direction": "increased"},
                ]
            },
            "review_ground_check": {"results": [{"name": "Fabricated Bug", "in_source": False}]},
        }
    )

    out, flags = _review(signatures, model)

    all_names = {t.taxon_name for sig in out for t in sig.taxa}
    assert all_names == {"Faecalibacterium prausnitzii"}
    assert "Fabricated Bug" not in all_names


def test_extractor_only_taxon_kept_when_it_re_grounds():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {"taxa": []},  # reviewer missed it entirely
            "review_ground_check": {"results": [{"name": "Faecalibacterium prausnitzii", "in_source": True}]},
        }
    )

    out, flags = _review(signatures, model)

    all_names = {t.taxon_name for sig in out for t in sig.taxa}
    assert all_names == {"Faecalibacterium prausnitzii"}
    assert flags == ()


def test_extractor_only_taxon_dropped_when_it_fails_to_re_ground():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "review_signature": {"taxa": []},
            "review_ground_check": {"results": [{"name": "Faecalibacterium prausnitzii", "in_source": False}]},
        }
    )

    out, flags = _review(signatures, model)

    assert out == []
    assert any("Faecalibacterium prausnitzii" in f for f in flags)


def test_taxon_present_in_both_directions_survives_arbitration():
    """A taxon can legitimately appear in BOTH direction-signatures (schema-
    legal -- `signature.dedup_taxa` only dedupes within one direction, never
    across `increased`/`decreased`). `extractor_by_norm`/`reviewer_by_norm`
    must not silently overwrite one direction's entry with the other's
    before arbitration runs -- both should survive (here: the reviewer
    misses it entirely on both sides, so each direction goes through the
    extractor-only re-grounding path independently and both are kept)."""
    signatures = [
        _sig("increased", [("Escherichia coli", 562)]),
        _sig("decreased", [("Escherichia coli", 562)]),
    ]
    model = MockModel(
        responses={
            "review_signature": {"taxa": []},  # reviewer missed this taxon entirely
            "review_ground_check": {"results": [{"name": "Escherichia coli", "in_source": True}]},
        }
    )

    out, flags = _review(signatures, model)

    directions_with_taxon = {
        sig.direction for sig in out if any(t.taxon_name == "Escherichia coli" for t in sig.taxa)
    }
    assert directions_with_taxon == {"increased", "decreased"}
    assert flags == ()


def test_repair_round_cap_is_enforced_no_infinite_loop():
    signatures = [_sig("increased", [("X", None)])]
    model = MockModel(
        responses={
            "review_signature": {"taxa": [{"name": "X", "direction": "decreased"}]},
            "review_reconcile_direction": {"direction": "sideways"},
        }
    )

    _review(signatures, model, max_repair_rounds=3)

    reconcile_calls = [c for c in model.calls if c["stage"] == "review_reconcile_direction"]
    assert len(reconcile_calls) == 3  # capped exactly at max_repair_rounds
