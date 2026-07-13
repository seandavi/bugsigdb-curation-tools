"""Tests for the figure-extraction benchmark scorer."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from pathlib import Path

_SCORE = SourceFileLoader(
    "figbench_score",
    str(Path(__file__).resolve().parents[1] / "benchmarks/figure-extraction/score.py"),
).load_module()


def test_normalize_strips_double_and_single_underscore_rank_prefixes():
    assert _SCORE.normalize("g__Bacillus") == "bacillus"
    assert _SCORE.normalize("c_Alphaproteobacteria") == "alphaproteobacteria"
    assert _SCORE.normalize("s__Mycobacterium tuberculosis") == "mycobacterium tuberculosis"
    assert _SCORE.normalize("Streptococcus") == "streptococcus"
    assert _SCORE.normalize("Clostridium_sensu_stricto") == "clostridium sensu stricto"


def test_normalize_does_not_eat_real_names_starting_with_rank_letter():
    # "candidatus" starts with 'c' but has no underscore -> must not be stripped
    assert _SCORE.normalize("Candidatus Saccharibacteria") == "candidatus saccharibacteria"


def test_score_entry_perfect_match():
    entry = {
        "pmcid": "PMCX",
        "figure_type": "lefse_bar_LDA",
        "gold": [
            {"direction": "increased", "taxa": [{"name": "g__Fusobacterium"}]},
            {"direction": "decreased", "taxa": [{"name": "g__Streptococcus"}]},
        ],
    }
    pred = {
        "predicted": [
            {"direction": "increased", "taxa": ["Fusobacterium"]},
            {"direction": "decreased", "taxa": ["Streptococcus"]},
        ]
    }
    s = _SCORE.score_entry(entry, pred)
    assert s.n_gold == 2 and s.n_pred == 2 and s.tp_strict == 2
    assert s.dir_matched == 2 and s.dir_correct == 2


def test_score_entry_direction_flip_counts_taxa_but_not_direction():
    entry = {
        "pmcid": "PMCX",
        "figure_type": "lefse_bar_LDA",
        "gold": [{"direction": "increased", "taxa": [{"name": "g__Fusobacterium"}]}],
    }
    pred = {"predicted": [{"direction": "decreased", "taxa": ["Fusobacterium"]}]}
    s = _SCORE.score_entry(entry, pred)
    assert s.tp_strict == 1  # taxon found
    assert s.dir_matched == 1 and s.dir_correct == 0  # direction wrong


def test_score_entry_unknown_direction_tracked_separately():
    entry = {
        "pmcid": "PMCX",
        "figure_type": "box_or_violin_with_stats",
        "gold": [{"direction": "increased", "taxa": [{"name": "g__Prevotella"}]}],
    }
    pred = {"predicted": [{"direction": "unknown", "taxa": ["Prevotella"]}]}
    s = _SCORE.score_entry(entry, pred)
    assert s.tp_strict == 1 and s.dir_unknown == 1 and s.dir_matched == 0


def test_genus_lenient_matches_species_to_genus():
    entry = {
        "pmcid": "PMCX",
        "figure_type": "cladogram",
        "gold": [{"direction": "increased", "taxa": [{"name": "s__Bacillus subtilis"}]}],
    }
    pred = {"predicted": [{"direction": "increased", "taxa": ["g__Bacillus"]}]}
    s = _SCORE.score_entry(entry, pred)
    assert s.tp_strict == 0  # species != genus exactly
    assert s.tp_genus == 1  # but same genus
