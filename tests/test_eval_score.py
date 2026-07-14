"""Unit tests for bugsigdb_curation.eval.score -- experiment alignment, signature
taxa-set scoring, direction correctness, name->ID sub-score, and the
known-bad-gold discount."""

from __future__ import annotations

from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy, source_type, to_nested_dict
from bugsigdb_curation.eval.score import (
    align_experiments,
    align_signatures,
    aggregate_scores,
    score_study,
)
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver


def _experiment(
    experiment_id: str = "1/Experiment 1",
    *,
    body_site=("Feces",),
    condition=("CRC",),
    group_0_name="healthy",
    group_1_name="CRC",
    sequencing_type="16S",
    statistical_test=(),
    mht_correction=None,
    signatures=(),
) -> GoldExperiment:
    return GoldExperiment(
        experiment_id=experiment_id,
        study_id="1",
        experiment_name=experiment_id.split("/")[-1],
        location_of_subjects=(),
        host_species="Homo sapiens",
        body_site=body_site,
        uberon_id=None,
        condition=condition,
        efo_id=None,
        group_0_name=group_0_name,
        group_1_name=group_1_name,
        group_1_definition=None,
        group_0_sample_size=None,
        group_1_sample_size=None,
        sequencing_type=sequencing_type,
        statistical_test=statistical_test,
        mht_correction=mht_correction,
        signatures=signatures,
    )


def _signature(
    signature_id: str,
    experiment_id: str = "1/Experiment 1",
    *,
    direction="increased",
    taxa=frozenset(),
    source="table 1",
    curation_state="Complete",
) -> GoldSignature:
    return GoldSignature(
        signature_id=signature_id,
        experiment_id=experiment_id,
        source=source,
        source_type=source_type(source),
        direction=direction,
        taxa=taxa,
        curation_state=curation_state,
    )


def _study(experiments, *, study_id="1", has_pmc=True) -> GoldStudy:
    return GoldStudy(
        study_id=study_id,
        pmid=study_id,
        doi=None,
        title=None,
        journal=None,
        year=None,
        study_design=(),
        pmcid=None,
        has_pmc=has_pmc,
        experiments=tuple(experiments),
    )


def _pred_exp(
    body_site=("Feces",),
    condition=("CRC",),
    group_0_name="healthy",
    group_1_name="CRC",
    sequencing_type="16S",
    signatures=(),
):
    return {
        "body_site": list(body_site),
        "condition": list(condition),
        "group_0_name": group_0_name,
        "group_1_name": group_1_name,
        "sequencing_type": sequencing_type,
        "signatures": list(signatures),
    }


def _pred_sig(direction="increased", taxa=(), taxon_names=()):
    taxa_list = [{"ncbi_id": t} for t in taxa] + [{"taxon_name": n} for n in taxon_names]
    return {"abundance_in_group_1": direction, "taxa": taxa_list}


# --- align_experiments: matched / over / under segmentation --------------------------------


def test_align_experiments_perfect_match():
    gold = [_experiment("1/Experiment 1")]
    pred = [_pred_exp()]
    alignment = align_experiments(gold, pred)
    assert alignment.matched == [(0, 0)]
    assert alignment.unmatched_pred == []
    assert alignment.unmatched_gold == []
    assert alignment.over_segmentation == 0
    assert alignment.under_segmentation == 0


def test_align_experiments_over_segmentation():
    # 1 gold experiment, pipeline predicted 3 -> over-segmentation, reported
    # separately from field accuracy (only 1 of the 3 can match).
    gold = [_experiment("1/Experiment 1")]
    pred = [_pred_exp(), _pred_exp(body_site=("Skin",)), _pred_exp(body_site=("Oral",))]
    alignment = align_experiments(gold, pred)
    assert len(alignment.matched) == 1
    assert alignment.over_segmentation == 2
    assert alignment.under_segmentation == 0
    assert len(alignment.unmatched_pred) == 2


def test_align_experiments_under_segmentation():
    gold = [_experiment("1/Experiment 1"), _experiment("1/Experiment 2", body_site=("Skin",))]
    pred = [_pred_exp()]
    alignment = align_experiments(gold, pred)
    assert alignment.over_segmentation == 0
    assert alignment.under_segmentation == 1
    assert len(alignment.unmatched_gold) == 1


def test_align_experiments_poor_overlap_pair_not_counted_as_matched():
    # A wildly different predicted experiment should NOT count as "matched"
    # just because Hungarian must assign every row to some column -- the
    # match-quality threshold keeps a near-zero-overlap pairing out of
    # `matched` and reports it as unmatched/unmatched instead.
    gold = [_experiment("1/Experiment 1", body_site=("Feces",), condition=("CRC",), group_0_name="a", group_1_name="b")]
    pred = [_pred_exp(body_site=("Skin",), condition=("Psoriasis",), group_0_name="x", group_1_name="y", sequencing_type="WMS")]
    alignment = align_experiments(gold, pred)
    assert alignment.matched == []
    assert alignment.unmatched_pred == [0]
    assert alignment.unmatched_gold == [0]


def test_align_experiments_field_accuracy_only_over_matched_pairs():
    gold = [
        _experiment(
            "1/Experiment 1", body_site=("Feces",), condition=("Obesity",),
            group_0_name="A0", group_1_name="B0", sequencing_type="16S",
        ),
        _experiment(
            "1/Experiment 2", body_site=("Skin",), condition=("Psoriasis",),
            group_0_name="A1", group_1_name="B1", sequencing_type="WMS",
        ),
    ]
    # Predicted experiment clearly resembles gold experiment 2 (matches
    # body_site/condition/groups) but got sequencing_type wrong; no
    # prediction resembles gold experiment 1 at all.
    pred = [
        _pred_exp(
            body_site=("Skin",), condition=("Psoriasis",),
            group_0_name="A1", group_1_name="B1", sequencing_type="16S",
        )
    ]
    alignment = align_experiments(gold, pred)
    assert alignment.matched == [(0, 1)]
    assert alignment.field_accuracy["body_site"] == 1.0
    assert alignment.field_accuracy["sequencing_type"] == 0.0


def test_matched_field_accuracy_excludes_both_blank_pairs_from_denominator():
    # Neither gold nor the prediction sets mht_correction (~8.4% of gold is
    # blank there) -- under the old behavior `None == None` counted as a
    # "correct" match, inflating accuracy for a field the pipeline never
    # even attempted. It must now be excluded from the denominator entirely
    # rather than credited as free-and-perfect.
    gold = [_experiment("1/Experiment 1", mht_correction=None)]
    pred = [_pred_exp()]  # no "mht_correction" key at all -> p.get(...) is None
    alignment = align_experiments(gold, pred)
    assert alignment.matched == [(0, 0)]
    assert alignment.field_accuracy["mht_correction"] == 0.0


# --- align_signatures: taxa-content based, not direction-label based -----------------------


def test_align_signatures_matches_by_taxa_overlap_not_direction_label():
    gold = [
        _signature("s1", direction="increased", taxa=frozenset({1, 2, 3})),
        _signature("s2", direction="decreased", taxa=frozenset({4, 5})),
    ]
    # Predicted signatures have SWAPPED direction labels relative to gold, but
    # their taxa content matches gold's increased/decreased sets respectively.
    pred = [
        {"abundance_in_group_1": "decreased", "taxa": [{"ncbi_id": 1}, {"ncbi_id": 2}, {"ncbi_id": 3}]},
        {"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": 4}, {"ncbi_id": 5}]},
    ]
    resolver = TaxonomyResolver()
    pairs = align_signatures(gold, pred, resolver)
    # gold index 0 (taxa {1,2,3}) should pair with pred index 0 (same taxa),
    # even though pred index 0 is labeled "decreased" not "increased".
    assert (0, 0) in pairs
    assert (1, 1) in pairs


def test_align_signatures_zero_overlap_pair_not_counted_as_matched():
    # Exactly one gold signature and one predicted signature, with
    # completely disjoint taxa -- the Hungarian solver has no other
    # candidate to try and would otherwise be forced to call this "matched"
    # (see SIGNATURE_MATCH_THRESHOLD's docstring for why that's unsafe: it
    # would inject a coin-flip direction comparison between two unrelated
    # signatures).
    gold = [_signature("s1", direction="increased", taxa=frozenset({1, 2, 3}))]
    pred = [{"abundance_in_group_1": "decreased", "taxa": [{"ncbi_id": 100}, {"ncbi_id": 200}]}]
    resolver = TaxonomyResolver()

    pairs = align_signatures(gold, pred, resolver)

    assert (0, 0) not in pairs
    assert (None, 0) in pairs  # predicted signature reported unmatched
    assert (0, None) in pairs  # gold signature reported unmatched


# --- score_study: taxa P/R/F1/Jaccard micro+macro -------------------------------------------


def test_score_study_perfect_prediction_scores_one():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1, 2})),))
    gold = _study([exp])
    resolver = TaxonomyResolver()
    predicted = to_nested_dict(gold)

    result = score_study(gold, predicted, resolver)

    assert result.micro_taxa.precision == 1.0
    assert result.micro_taxa.recall == 1.0
    assert result.micro_taxa.f1 == 1.0
    assert result.micro_taxa.jaccard == 1.0
    assert result.macro_taxa_f1 == 1.0
    assert result.direction_correct == result.direction_total == 1
    assert len(result.experiment_alignment.matched) == 1


def test_score_study_partial_recall_and_precision():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1, 2, 3})),))
    gold = _study([exp])
    # Predicted found 1 and 2 (missed 3: recall miss) and also predicted 99 (extra: precision miss).
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxa=[1, 2, 99])])]}
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver)

    assert result.micro_taxa.tp == 2
    assert result.micro_taxa.fp == 1
    assert result.micro_taxa.fn == 1
    assert result.micro_taxa.precision == 2 / 3
    assert result.micro_taxa.recall == 2 / 3


def test_score_study_missing_prediction_scores_zero_recall():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1, 2})),))
    gold = _study([exp])
    resolver = TaxonomyResolver()

    result = score_study(gold, None, resolver)

    assert result.micro_taxa.tp == 0
    assert result.micro_taxa.fn == 2
    assert result.micro_taxa.recall == 0.0
    assert result.experiment_alignment.under_segmentation == 1


def test_score_study_zero_overlap_signature_pair_symmetric_fn_fp_no_direction_credit():
    exp = _experiment(signatures=(_signature("s1", direction="increased", taxa=frozenset({1, 2, 3})),))
    gold = _study([exp])
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(direction="decreased", taxa=[100, 200])])]}
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver)

    # Below SIGNATURE_MATCH_THRESHOLD: the pair is NOT "matched", so no
    # direction comparison happens for it at all -- not even as a miss.
    assert result.direction_total == 0
    # Gold's 3 taxa are all FN, pred's 2 taxa are all FP -- symmetric, as if
    # these were two entirely separate unmatched signatures (already-safe
    # per SIGNATURE_MATCH_THRESHOLD's docstring).
    assert result.micro_taxa.tp == 0
    assert result.micro_taxa.fn == 3
    assert result.micro_taxa.fp == 2


# --- direction correctness ------------------------------------------------------------------


def test_score_study_direction_correctness_with_systematic_flip():
    exp = _experiment(
        signatures=(
            _signature("s1", direction="increased", taxa=frozenset({1, 2})),
            _signature("s2", direction="decreased", taxa=frozenset({3, 4})),
        )
    )
    gold = _study([exp])
    # Correct taxa content, but every direction label flipped.
    pred = {
        "experiments": [
            _pred_exp(
                signatures=[
                    _pred_sig(direction="decreased", taxa=[1, 2]),
                    _pred_sig(direction="increased", taxa=[3, 4]),
                ]
            )
        ]
    }
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver)

    # Taxa content is still found perfectly (alignment is by content)...
    assert result.micro_taxa.f1 == 1.0
    # ...but direction correctness reveals the systematic flip.
    assert result.direction_correct == 0
    assert result.direction_total == 2


# --- name -> ID sub-score --------------------------------------------------------------------


def test_name_to_id_subscore_counts_found_and_correct():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({561})),))
    gold = _study([exp])
    resolver = TaxonomyResolver(cache={"escherichia coli": 561, "shigella": 620})
    resolver.id_to_name[561] = "escherichia coli"

    # Predicted the right name but resolves (via the cache) to the right id.
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxon_names=["Escherichia coli"])])]}
    result = score_study(gold, pred, resolver)
    assert result.name_to_id_found == 1
    assert result.name_to_id_correct == 1


def test_name_to_id_subscore_wrong_mapping_not_counted_correct():
    # Gold has two taxa; the prediction correctly finds one (620, by id) so
    # the pair clears SIGNATURE_MATCH_THRESHOLD and aligns as a matched
    # signature -- a *single*-taxon signature that's 100% wrong would be a
    # zero-overlap pair the alignment floor correctly excludes (see the
    # dedicated sub-floor test below), which isn't what this test means to
    # exercise.
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({561, 620})),))
    gold = _study([exp])
    resolver = TaxonomyResolver(cache={"escherichia coli": 561, "shigella": 620})
    resolver.id_to_name[561] = "escherichia coli"
    resolver.id_to_name[620] = "shigella"

    # Predicted taxon carries the RIGHT name string but a WRONG hardcoded id
    # (simulating a mis-mapping / hallucinated id despite finding the taxon).
    pred = {
        "experiments": [
            _pred_exp(
                signatures=[
                    {
                        "abundance_in_group_1": "increased",
                        "taxa": [{"ncbi_id": 620}, {"taxon_name": "Escherichia coli", "ncbi_id": 99999}],
                    }
                ]
            )
        ]
    }
    result = score_study(gold, pred, resolver)
    assert result.name_to_id_found == 1
    assert result.name_to_id_correct == 0


# --- known-bad-gold discount (blank curation_state -- the real corpus never emits the literal
# "Incomplete" string; 406/14,156 gold signatures have a blank `State` cell instead, all with zero
# gold taxa. See score.py's module docstring / `_is_known_bad_gold`.) ---------------------------


def test_blank_curation_state_gold_signature_discounts_false_positives():
    # curation_state=None is the REAL "not complete" signal in the export
    # (a blank `State` cell), not the literal string "Incomplete".
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1}), curation_state=None),))
    gold = _study([exp])
    # Predicted taxon 1 (correct) plus an extra taxon 2 not in the (possibly
    # incomplete) gold set -- with discounting, taxon 2 should not count as FP.
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxa=[1, 2])])]}
    resolver = TaxonomyResolver()

    discounted = score_study(gold, pred, resolver, discount_incomplete=True)
    undiscounted = score_study(gold, pred, resolver, discount_incomplete=False)

    assert discounted.micro_taxa.fp == 0
    assert discounted.micro_taxa.precision == 1.0
    assert undiscounted.micro_taxa.fp == 1
    assert undiscounted.micro_taxa.precision == 0.5


def test_schema_documented_incomplete_string_still_discounts():
    # The literal string never occurs in the real export, but the schema
    # documents it as a valid `State` value -- kept as an alternate spelling
    # in case a future export starts emitting it (see `_is_known_bad_gold`).
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1}), curation_state="Incomplete"),))
    gold = _study([exp])
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxa=[1, 2])])]}
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver, discount_incomplete=True)
    assert result.micro_taxa.fp == 0


def test_complete_gold_signature_not_discounted():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1}), curation_state="Complete"),))
    gold = _study([exp])
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxa=[1, 2])])]}
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver, discount_incomplete=True)
    assert result.micro_taxa.fp == 1


# --- blank gold direction (~3% of gold signatures) excluded from direction_total, not an
# automatic miss -----------------------------------------------------------------------------


def test_blank_gold_direction_excluded_from_direction_total():
    exp = _experiment(
        signatures=(
            _signature("s1", direction=None, taxa=frozenset({1, 2})),
            _signature("s2", direction="decreased", taxa=frozenset({3, 4})),
        )
    )
    gold = _study([exp])
    pred = {
        "experiments": [
            _pred_exp(
                signatures=[
                    _pred_sig(direction="increased", taxa=[1, 2]),
                    _pred_sig(direction="decreased", taxa=[3, 4]),
                ]
            )
        ]
    }
    resolver = TaxonomyResolver()

    result = score_study(gold, pred, resolver)

    # s1's taxa still score normally (matched, perfect taxa overlap) -- only
    # the direction comparison is skipped because gold's direction is blank.
    assert result.micro_taxa.f1 == 1.0
    # Only s2 (non-blank gold direction) counts toward direction_total; s1's
    # blank direction contributes neither a hit nor a miss.
    assert result.direction_total == 1
    assert result.direction_correct == 1


# --- aggregate_scores: source-type / experiment-count stratification -------------------------


def test_aggregate_scores_stratifies_by_source_type():
    exp1 = _experiment(
        "1/Experiment 1", signatures=(_signature("s1", "1/Experiment 1", taxa=frozenset({1}), source="table 1"),)
    )
    study1 = _study([exp1], study_id="1")
    pred1 = to_nested_dict(study1)

    exp2 = _experiment(
        "2/Experiment 1", signatures=(_signature("s2", "2/Experiment 1", taxa=frozenset({2}), source="Figure 3"),)
    )
    study2 = _study([exp2], study_id="2")
    pred2 = {"experiments": [_pred_exp(signatures=[_pred_sig(taxa=[])])]}  # miss everything

    resolver = TaxonomyResolver()
    scores = [score_study(study1, pred1, resolver), score_study(study2, pred2, resolver)]
    aggregate = aggregate_scores(scores)

    assert aggregate.by_source_type["main-table"].recall == 1.0
    assert aggregate.by_source_type["figure"].recall == 0.0


def test_aggregate_scores_direction_accuracy_and_name_to_id_accuracy():
    exp = _experiment(signatures=(_signature("s1", direction="increased", taxa=frozenset({1})),))
    gold = _study([exp])
    pred = to_nested_dict(gold)
    resolver = TaxonomyResolver()

    scores = [score_study(gold, pred, resolver)]
    aggregate = aggregate_scores(scores)
    assert aggregate.direction_accuracy == 1.0
    assert aggregate.n_studies == 1


# --- resolution-coverage counters (Fix 2b) ---------------------------------------------------


def test_n_unresolved_pred_taxa_counts_unresolvable_names():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1})),))
    gold = _study([exp])
    resolver = TaxonomyResolver()  # no db, no cache -- nothing resolves
    pred = {"experiments": [_pred_exp(signatures=[_pred_sig(taxon_names=["Some Unresolvable Organism"])])]}

    result = score_study(gold, pred, resolver)

    assert result.n_unresolved_pred_taxa == 1


def test_n_unresolved_gold_taxa_counts_ids_with_no_name():
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1, 2})),))
    gold = _study([exp])
    resolver = TaxonomyResolver()
    resolver.id_to_name[1] = "known organism"  # only id 1 has a name; id 2 doesn't
    pred = to_nested_dict(gold)

    result = score_study(gold, pred, resolver)

    assert result.n_unresolved_gold_taxa == 1


def test_n_unresolved_gold_taxa_counts_unmatched_gold_experiments_too():
    """The "every gold taxon is a full FN" branch for an unmatched gold
    experiment (score_study's `alignment.unmatched_gold` loop) must count
    resolution failures too, not just the matched-experiment path through
    `score_experiment_signatures`."""
    exp = _experiment(signatures=(_signature("s1", taxa=frozenset({1, 2})),))
    gold = _study([exp])
    resolver = TaxonomyResolver()
    resolver.id_to_name[1] = "known organism"

    result = score_study(gold, None, resolver)  # no prediction -> gold experiment unmatched

    assert result.n_unresolved_gold_taxa == 1


def test_aggregate_scores_rolls_up_resolution_coverage_counts():
    exp1 = _experiment("1/Experiment 1", signatures=(_signature("s1", "1/Experiment 1", taxa=frozenset({1})),))
    study1 = _study([exp1], study_id="1")
    pred1 = {"experiments": [_pred_exp(signatures=[_pred_sig(taxon_names=["Unresolvable One"])])]}

    exp2 = _experiment("2/Experiment 1", signatures=(_signature("s2", "2/Experiment 1", taxa=frozenset({2})),))
    study2 = _study([exp2], study_id="2")
    pred2 = {"experiments": [_pred_exp(signatures=[_pred_sig(taxon_names=["Unresolvable Two"])])]}

    resolver = TaxonomyResolver()
    scores = [score_study(study1, pred1, resolver), score_study(study2, pred2, resolver)]
    aggregate = aggregate_scores(scores)

    assert aggregate.n_unresolved_pred_taxa == 2
    assert aggregate.n_unresolved_gold_taxa == 2  # taxa 1 and 2, neither has a name


def test_aggregate_scores_rolls_up_segmentation_totals_across_studies():
    # Study 1: 1 gold experiment, pipeline predicted 3 -> over-segmentation 2.
    exp1 = _experiment("1/Experiment 1")
    study1 = _study([exp1], study_id="1")
    pred1 = {
        "experiments": [
            _pred_exp(), _pred_exp(body_site=("Skin",)), _pred_exp(body_site=("Oral",))
        ]
    }
    # Study 2: 2 gold experiments, pipeline predicted 1 -> under-segmentation 1.
    exp2a = _experiment("2/Experiment 1")
    exp2b = _experiment("2/Experiment 2", body_site=("Skin",))
    study2 = _study([exp2a, exp2b], study_id="2")
    pred2 = {"experiments": [_pred_exp()]}

    resolver = TaxonomyResolver()
    scores = [score_study(study1, pred1, resolver), score_study(study2, pred2, resolver)]
    aggregate = aggregate_scores(scores)

    assert aggregate.over_segmentation == 2
    assert aggregate.under_segmentation == 1
