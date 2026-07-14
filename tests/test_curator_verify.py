"""Unit tests for `bugsigdb_curation.curator.verify` (split-verify's S10
adversarial verifier: taxon-in-source grounding + bounded direction repair)."""

from __future__ import annotations

from bugsigdb_curation.curator.evidence import EvidenceTable
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon
from bugsigdb_curation.curator.verify import check_taxa_in_source, verify_signatures

_TABLE = EvidenceTable(
    table_id="T2", number="2", label="Table 2.", caption="DA taxa.",
    rows=(("Taxon", "Direction"), ("Faecalibacterium prausnitzii", "decreased"), ("Escherichia coli", "increased")),
)
_ARTIFACT = LocatedArtifact(kind="table", table=_TABLE)


def _sig(direction, taxa):
    return ExtractedSignature(
        direction=direction, taxa=tuple(ExtractedTaxon(taxon_name=n, direction=direction, ncbi_id=i) for n, i in taxa)
    )


# --- check_taxa_in_source -----------------------------------------------------------------------


def test_check_taxa_in_source_returns_only_confirmed_names():
    model = MockModel(
        responses={
            "verify_taxon_in_source": {
                "results": [
                    {"name": "Real Taxon", "in_source": True},
                    {"name": "Invented Taxon", "in_source": False},
                ]
            }
        }
    )
    grounded = check_taxa_in_source(["Real Taxon", "Invented Taxon"], "source text", model=model)
    assert grounded == {"Real Taxon"}


def test_check_taxa_in_source_empty_list_makes_no_call():
    model = MockModel()
    assert check_taxa_in_source([], "source text", model=model) == set()
    assert model.calls == []


# --- verify_signatures ---------------------------------------------------------------------------


def test_verify_signatures_drops_taxon_the_verifier_says_is_not_in_source():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)]), _sig("increased", [("Invented Bug", None)])]
    model = MockModel(
        responses={
            "verify_taxon_in_source": {
                "results": [
                    {"name": "Faecalibacterium prausnitzii", "in_source": True},
                    {"name": "Invented Bug", "in_source": False},
                ]
            },
            "verify_direction": {"direction": "decreased"},  # confirms the one surviving taxon's claim
        }
    )

    out, flags = verify_signatures(signatures, artifact=_ARTIFACT, model=model)

    all_names = {t.taxon_name for sig in out for t in sig.taxa}
    assert all_names == {"Faecalibacterium prausnitzii"}
    assert any("Invented Bug" in f and "not confirmed in source" in f for f in flags)


def test_verify_signatures_flips_a_wrong_direction_on_re_derivation():
    """The extractor claimed "increased"; the verifier's independent
    re-derivation (stable across 2 rounds) says "decreased" -- the final
    signature must carry the corrected direction, not the extractor's."""
    signatures = [_sig("increased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "verify_taxon_in_source": {"results": [{"name": "Faecalibacterium prausnitzii", "in_source": True}]},
            "verify_direction": {"direction": "decreased"},
        }
    )

    out, flags = verify_signatures(signatures, artifact=_ARTIFACT, model=model)

    assert len(out) == 1
    assert out[0].direction == "decreased"
    assert out[0].taxa[0].taxon_name == "Faecalibacterium prausnitzii"
    assert out[0].taxa[0].ncbi_id == 853  # id preserved across the flip
    assert flags == ()  # a successful repair isn't a flag -- only exhaustion is


def test_verify_signatures_keeps_confirmed_direction_unchanged():
    signatures = [_sig("decreased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "verify_taxon_in_source": {"results": [{"name": "Faecalibacterium prausnitzii", "in_source": True}]},
            "verify_direction": {"direction": "decreased"},
        }
    )

    out, flags = verify_signatures(signatures, artifact=_ARTIFACT, model=model)
    assert out[0].direction == "decreased"
    assert flags == ()


def test_verify_signatures_flags_and_drops_a_taxon_whose_direction_never_converges():
    signatures = [_sig("increased", [("Faecalibacterium prausnitzii", 853)])]
    model = MockModel(
        responses={
            "verify_taxon_in_source": {"results": [{"name": "Faecalibacterium prausnitzii", "in_source": True}]},
            "verify_direction": {"direction": "sideways"},  # never parseable -> never converges
        }
    )

    out, flags = verify_signatures(signatures, artifact=_ARTIFACT, model=model, max_repair_rounds=2)

    assert out == []
    assert any("unresolved" in f for f in flags)


def test_verify_signatures_empty_input_short_circuits():
    model = MockModel()
    out, flags = verify_signatures([], artifact=_ARTIFACT, model=model)
    assert out == []
    assert flags == ()
    assert model.calls == []
