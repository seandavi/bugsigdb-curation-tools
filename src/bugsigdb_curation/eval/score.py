"""Score de-novo curator predictions (loader nested-dict shape) against gold.

Consumes predictions in exactly the shape `bugsigdb_curation.loader` emits --
`Study -> experiments[] -> signatures[] -> taxa[]` -- so the same contract
that round-trips `full_dump.csv` also round-trips a pipeline's output. A
prediction taxon may carry `ncbi_id` and/or `taxon_name`; see
`bugsigdb_curation.eval.taxonomy` for how those get turned into a taxid.

Two deliberate design choices worth flagging to a reviewer:

* **Signature alignment is by taxa-set overlap, not by the declared
  `abundance_in_group_1` label.** A literal reading of the plan ("align
  signatures by direction") would pair every predicted "increased" signature
  with the gold "increased" signature regardless of content. That makes a
  *systematic* direction flip (Group 0/1 orientation swapped) look like a
  catastrophic taxa-recall failure instead of what it actually is: the right
  taxa, wrong label. Aligning by taxa-set Jaccard overlap instead (same
  Hungarian machinery as experiment alignment, just tiny matrices since
  there are normally <=2 signatures/experiment) lets a flip surface cleanly
  as low `direction_correct` while the taxa P/R/F1 stays high -- which is
  the whole point of reporting the two separately (§4d).
* **The "known-bad gold" discount (§4d) uses a blank `curation_state` as its
  signal**, not a per-taxon "missing ncbi_id" flag. The relational export has
  no such flag: `signatures_taxa.csv` rows without a resolvable `ncbi_id`
  simply don't exist, so there's nothing at the taxon level to key off of.
  The schema documents `curation_state` as `"Complete"` / `"Incomplete"`, but
  the real export **never emits the literal string `"Incomplete"`** -- "not
  complete" is represented by leaving the `State` cell blank, which
  `bugsigdb_curation.eval.gold` parses as `curation_state is None` (in the
  corpus: 13,750 `"Complete"` rows vs. 406 blank/`None` rows, all of which
  have zero gold taxa -- exactly the stub case this discount is meant to
  exempt). A gold signature counts as "known-bad" when `curation_state is
  None` **or** the schema-documented `"Incomplete"` string (kept in case a
  future export starts emitting it) -- see `_is_known_bad_gold`. When
  discounting, a predicted taxon that doesn't match a known-bad signature's
  gold set is excluded from that signature's false-positive count (not
  penalized), while true positives and recall are scored normally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bugsigdb_curation.eval.assignment import assign_max_weight
from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy, SourceType
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver, normalize_taxon_name

#: Weight of the signature-content tie-breaker added to
#: `_experiment_field_overlap` (see its docstring). Deliberately tiny: it
#: must never outrank a genuine metadata difference (each of the four
#: metadata components moves the average by ~0.25), it only needs to break
#: ties between metadata-identical candidates.
_CONTENT_TIEBREAK_WEIGHT = 0.01

#: Minimum field-overlap score (see `_experiment_field_overlap`) for a
#: Hungarian-optimal predicted<->gold experiment pair to count as "matched"
#: rather than as one unmatched-pred + one unmatched-gold. Without this
#: floor, the optimal assignment still pairs up every row/column even when
#: the best available pairing is a near-zero-overlap non-match (e.g. 6
#: predicted experiments against 1 wildly different gold experiment) --
#: which would silently count as "matched" and pollute the matched-pair
#: field-accuracy numbers.
EXPERIMENT_MATCH_THRESHOLD = 0.15


# ---------------------------------------------------------------------------
# result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PRF1:
    """Precision/recall/F1/Jaccard over a set-comparison, plus the raw counts."""

    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    jaccard: float


def prf1(tp: int, fp: int, fn: int) -> PRF1:
    """Build a `PRF1` from raw true/false-positive/negative counts."""
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    union = tp + fp + fn
    jaccard = tp / union if union else 0.0
    return PRF1(tp=tp, fp=fp, fn=fn, precision=precision, recall=recall, f1=f1, jaccard=jaccard)


@dataclass(frozen=True, slots=True)
class ExperimentAlignment:
    """Predicted<->gold experiment alignment for one study.

    `over_segmentation`/`under_segmentation` are the plan's "experiment-count
    delta, reported separately from field accuracy" (§4b): a predicted count
    higher than gold's suggests the pipeline split a true experiment into
    several (over-segmentation); lower suggests it merged several into one
    (under-segmentation). `field_accuracy` is scored ONLY over `matched`
    pairs, so a segmentation error never masquerades as a field error.
    """

    n_pred: int
    n_gold: int
    matched: list[tuple[int, int]]  # (pred_index, gold_index)
    unmatched_pred: list[int]
    unmatched_gold: list[int]
    over_segmentation: int
    under_segmentation: int
    field_accuracy: dict[str, float]


@dataclass(frozen=True, slots=True)
class StudyScore:
    """Full scoring result for one study."""

    study_id: str
    has_pmc: bool | None
    n_gold_experiments: int
    n_pred_experiments: int
    experiment_alignment: ExperimentAlignment
    micro_taxa: PRF1
    macro_taxa_f1: float
    micro_genus: PRF1
    macro_genus_f1: float
    direction_correct: int
    direction_total: int
    name_to_id_correct: int
    name_to_id_found: int
    n_unresolved_pred_taxa: int
    source_type_counts: dict[str, int]
    #: (gold source_type or None, PRF1) for every scored signature pair --
    #: the raw material `aggregate_scores` regroups by source type globally.
    signature_pairs: tuple[tuple[str | None, PRF1], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AggregateScore:
    """Corpus-level (or smoke-set-level) aggregate over many `StudyScore`s."""

    n_studies: int
    micro_taxa: PRF1
    macro_taxa_f1: float
    direction_accuracy: float
    name_to_id_accuracy: float
    by_source_type: dict[str, PRF1]
    by_experiment_bucket: dict[str, PRF1]
    #: Corpus-level roll-up of every study's `ExperimentAlignment.over_segmentation`
    #: / `.under_segmentation` (§4b wants this reportable at corpus level, not
    #: just per-study).
    over_segmentation: int
    under_segmentation: int


EXPERIMENT_COUNT_BUCKETS = ("1", "2-5", "6-20", "21+")


def experiment_count_bucket(n: int) -> str:
    """Bucket a study's gold experiment count for stratified reporting."""
    if n <= 1:
        return "1"
    if n <= 5:
        return "2-5"
    if n <= 20:
        return "6-20"
    return "21+"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _norm_str(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_set(values: list[str] | tuple[str, ...]) -> frozenset[str]:
    return frozenset(_norm_str(v) for v in values if v and v.strip())


def _pred_str_list(exp: dict[str, Any], key: str) -> list[str]:
    value = exp.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _jaccard_or_none(a: frozenset[str], b: frozenset[str]) -> float | None:
    """Jaccard overlap, or None if BOTH sides are empty (neutral: no signal)."""
    if not a and not b:
        return None
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _micro(items: list[PRF1]) -> PRF1:
    return prf1(sum(p.tp for p in items), sum(p.fp for p in items), sum(p.fn for p in items))


def _is_known_bad_gold(gold_sig: GoldSignature | None) -> bool:
    """True when a gold signature is the "known-bad" stub the §4d discount is
    meant to exempt.

    The real relational export represents "not complete" as a **blank**
    `State` cell, which `eval.gold` parses as `curation_state is None` --
    the schema-documented literal string `"Incomplete"` never actually
    occurs in the corpus (see this module's docstring), but is still
    recognized here in case a future export starts emitting it.
    """
    if gold_sig is None:
        return False
    return gold_sig.curation_state is None or gold_sig.curation_state == "Incomplete"


# ---------------------------------------------------------------------------
# experiment alignment (§4b)
# ---------------------------------------------------------------------------


def _experiment_field_overlap(
    gold: GoldExperiment, pred: dict[str, Any], resolver: TaxonomyResolver | None = None
) -> float:
    """Overlap score in [0, 1] used as the Hungarian assignment weight.

    A weighted average of four component scores -- body_site Jaccard,
    condition Jaccard, group-name-set Jaccard (the two comparison groups'
    semantics), and an exact sequencing_type match -- each in [0, 1]. A
    component missing on BOTH sides is excluded from the average (neutral),
    not scored as a mismatch, so e.g. two experiments that both simply lack
    a `sequencing_type` aren't penalized for it.

    If `resolver` is given, a small (`_CONTENT_TIEBREAK_WEIGHT`) bonus is
    added for signature-taxa-set overlap between the two experiments' pooled
    signatures. This exists to break ties among experiments whose §1b
    metadata is genuinely indistinguishable -- found via this module's own
    perfect-round-trip sanity check on study 34963452, whose 200 experiments
    all share the identical `(body_site="Feces", condition=(), ...)` tuple
    (only the per-experiment taxa differ). Without it, the four metadata
    components alone can't disambiguate such experiments and the Hungarian
    solver picks an arbitrary tied assignment, scrambling signature content
    across otherwise-correct experiments. The weight is small enough that it
    can only ever break a tie, never override a genuine metadata mismatch.
    """
    components: list[float] = []

    body_j = _jaccard_or_none(_norm_set(gold.body_site), _norm_set(_pred_str_list(pred, "body_site")))
    if body_j is not None:
        components.append(body_j)

    cond_j = _jaccard_or_none(_norm_set(gold.condition), _norm_set(_pred_str_list(pred, "condition")))
    if cond_j is not None:
        components.append(cond_j)

    gold_groups = _norm_set([gold.group_0_name or "", gold.group_1_name or ""])
    pred_groups = _norm_set([str(pred.get("group_0_name") or ""), str(pred.get("group_1_name") or "")])
    group_j = _jaccard_or_none(gold_groups, pred_groups)
    if group_j is not None:
        components.append(group_j)

    gold_seq = gold.sequencing_type
    pred_seq = pred.get("sequencing_type")
    if gold_seq or pred_seq:
        components.append(1.0 if gold_seq and pred_seq and gold_seq == pred_seq else 0.0)

    score = _mean(components)

    if resolver is not None:
        gold_taxa: set[int] = set()
        for sig in gold.signatures:
            gold_taxa |= sig.taxa
        pred_taxa: set[int] = set()
        for sig in pred.get("signatures", []) or []:
            ids, _ = _resolve_pred_taxa(sig.get("taxa", []) or [], resolver)
            pred_taxa |= ids
        content_j = _taxa_jaccard(frozenset(gold_taxa), frozenset(pred_taxa))
        score += _CONTENT_TIEBREAK_WEIGHT * content_j

    return score


_MATCHED_FIELD_NAMES = ("body_site", "condition", "sequencing_type", "statistical_test", "mht_correction", "group_orientation")


def _matched_field_accuracy(
    gold_experiments: list[GoldExperiment],
    pred_experiments: list[dict[str, Any]],
    matched: list[tuple[int, int]],
) -> dict[str, float]:
    """Per-field accuracy over MATCHED experiment pairs only (§1b fields)."""
    if not matched:
        return dict.fromkeys(_MATCHED_FIELD_NAMES, 0.0)

    correct = dict.fromkeys(_MATCHED_FIELD_NAMES, 0)
    for pred_idx, gold_idx in matched:
        g = gold_experiments[gold_idx]
        p = pred_experiments[pred_idx]
        if _norm_set(g.body_site) == _norm_set(_pred_str_list(p, "body_site")):
            correct["body_site"] += 1
        if _norm_set(g.condition) == _norm_set(_pred_str_list(p, "condition")):
            correct["condition"] += 1
        if (g.sequencing_type or None) == (p.get("sequencing_type") or None):
            correct["sequencing_type"] += 1
        if _norm_set(g.statistical_test) == _norm_set(_pred_str_list(p, "statistical_test")):
            correct["statistical_test"] += 1
        if g.mht_correction == p.get("mht_correction"):
            correct["mht_correction"] += 1
        gold_orientation = (_norm_str(g.group_0_name), _norm_str(g.group_1_name))
        pred_orientation = (_norm_str(p.get("group_0_name")), _norm_str(p.get("group_1_name")))
        if gold_orientation == pred_orientation:
            correct["group_orientation"] += 1

    n = len(matched)
    return {field_name: correct[field_name] / n for field_name in _MATCHED_FIELD_NAMES}


def align_experiments(
    gold_experiments: list[GoldExperiment],
    pred_experiments: list[dict[str, Any]],
    *,
    threshold: float = EXPERIMENT_MATCH_THRESHOLD,
    resolver: TaxonomyResolver | None = None,
) -> ExperimentAlignment:
    """Bipartite-match predicted<->gold experiments by field overlap (§4b).

    Runs the Hungarian algorithm to maximize total overlap, then drops any
    optimal-but-poor pair below `threshold` back into unmatched/unmatched
    (see `EXPERIMENT_MATCH_THRESHOLD`'s docstring for why that floor exists).
    `resolver`, if given, enables the signature-content tie-breaker described
    in `_experiment_field_overlap`.
    """
    n_gold = len(gold_experiments)
    n_pred = len(pred_experiments)

    if n_gold == 0 or n_pred == 0:
        return ExperimentAlignment(
            n_pred=n_pred,
            n_gold=n_gold,
            matched=[],
            unmatched_pred=list(range(n_pred)),
            unmatched_gold=list(range(n_gold)),
            over_segmentation=max(0, n_pred - n_gold),
            under_segmentation=max(0, n_gold - n_pred),
            field_accuracy={},
        )

    scores = [[_experiment_field_overlap(g, p, resolver) for g in gold_experiments] for p in pred_experiments]
    assignment = assign_max_weight(scores)  # length n_pred; assignment[i] = gold idx or None

    matched: list[tuple[int, int]] = []
    used_gold: set[int] = set()
    for pred_idx, gold_idx in enumerate(assignment):
        if gold_idx is not None and scores[pred_idx][gold_idx] >= threshold:
            matched.append((pred_idx, gold_idx))
            used_gold.add(gold_idx)

    matched_pred_idxs = {p for p, _ in matched}
    unmatched_pred = [i for i in range(n_pred) if i not in matched_pred_idxs]
    unmatched_gold = [i for i in range(n_gold) if i not in used_gold]

    return ExperimentAlignment(
        n_pred=n_pred,
        n_gold=n_gold,
        matched=matched,
        unmatched_pred=unmatched_pred,
        unmatched_gold=unmatched_gold,
        over_segmentation=max(0, n_pred - n_gold),
        under_segmentation=max(0, n_gold - n_pred),
        field_accuracy=_matched_field_accuracy(gold_experiments, pred_experiments, matched),
    )


# ---------------------------------------------------------------------------
# signature / taxa scoring (§4b headline, §4c, §4d)
# ---------------------------------------------------------------------------


def _resolve_pred_taxa(taxa: list[dict[str, Any]], resolver: TaxonomyResolver) -> tuple[frozenset[int], int]:
    """Resolve a predicted taxa list to a taxid set; returns (ids, n_unresolved)."""
    ids: set[int] = set()
    unresolved = 0
    for taxon in taxa:
        resolved = resolver.resolve_taxon(taxon)
        if resolved is None:
            unresolved += 1
        else:
            ids.add(resolved)
    return frozenset(ids), unresolved


def _taxa_jaccard(gold_ids: frozenset[int], pred_ids: frozenset[int]) -> float:
    if not gold_ids and not pred_ids:
        return 1.0
    if not gold_ids or not pred_ids:
        return 0.0
    return len(gold_ids & pred_ids) / len(gold_ids | pred_ids)


def align_signatures(
    gold_signatures: list[GoldSignature],
    pred_signatures: list[dict[str, Any]],
    resolver: TaxonomyResolver,
) -> list[tuple[int | None, int | None]]:
    """Bipartite-match gold<->predicted signatures within one experiment.

    Matched by taxid-set Jaccard overlap, NOT by the declared direction label
    -- see this module's docstring for why. Returns `(gold_index,
    pred_index)` pairs; either side may be `None` for an unmatched signature.
    """
    n_gold = len(gold_signatures)
    n_pred = len(pred_signatures)
    if n_gold == 0 and n_pred == 0:
        return []
    if n_gold == 0:
        return [(None, i) for i in range(n_pred)]
    if n_pred == 0:
        return [(i, None) for i in range(n_gold)]

    pred_id_sets = [_resolve_pred_taxa(p.get("taxa", []) or [], resolver)[0] for p in pred_signatures]
    scores = [[_taxa_jaccard(g.taxa, pred_id_sets[i]) for g in gold_signatures] for i in range(n_pred)]
    assignment = assign_max_weight(scores)  # length n_pred

    pairs: list[tuple[int | None, int | None]] = []
    matched_gold: set[int] = set()
    for pred_idx, gold_idx in enumerate(assignment):
        if gold_idx is not None:
            pairs.append((gold_idx, pred_idx))
            matched_gold.add(gold_idx)
        else:
            pairs.append((None, pred_idx))
    for gold_idx in range(n_gold):
        if gold_idx not in matched_gold:
            pairs.append((gold_idx, None))
    return pairs


@dataclass(frozen=True, slots=True)
class _SignatureScoreDetail:
    pairs: list[tuple[SourceType | None, PRF1, PRF1]]  # (source_type, taxa PRF1, genus PRF1)
    direction_correct: int
    direction_total: int
    name_to_id_found: int
    name_to_id_correct: int
    n_unresolved_pred_taxa: int


def score_experiment_signatures(
    gold_signatures: list[GoldSignature],
    pred_signatures: list[dict[str, Any]],
    resolver: TaxonomyResolver,
    *,
    discount_incomplete: bool = True,
) -> _SignatureScoreDetail:
    """Score all signature pairs within one matched experiment."""
    alignment = align_signatures(gold_signatures, pred_signatures, resolver)

    pairs: list[tuple[SourceType | None, PRF1, PRF1]] = []
    direction_correct = 0
    direction_total = 0
    name_to_id_found = 0
    name_to_id_correct = 0
    n_unresolved = 0

    for gold_idx, pred_idx in alignment:
        gold_sig = gold_signatures[gold_idx] if gold_idx is not None else None
        pred_sig = pred_signatures[pred_idx] if pred_idx is not None else None

        gold_ids = gold_sig.taxa if gold_sig is not None else frozenset()
        pred_taxa_list = (pred_sig.get("taxa", []) or []) if pred_sig is not None else []
        pred_ids, unresolved = _resolve_pred_taxa(pred_taxa_list, resolver)
        n_unresolved += unresolved

        tp = len(gold_ids & pred_ids)
        fp = len(pred_ids - gold_ids)
        fn = len(gold_ids - pred_ids)
        if discount_incomplete and _is_known_bad_gold(gold_sig):
            fp = 0
        taxa_score = prf1(tp, fp, fn)

        gold_genus = frozenset(g for g in (resolver.genus_of_id(i) for i in gold_ids) if g)
        pred_genus = frozenset(g for g in (resolver.genus_of_id(i) for i in pred_ids) if g)
        g_tp = len(gold_genus & pred_genus)
        g_fp = len(pred_genus - gold_genus)
        g_fn = len(gold_genus - pred_genus)
        if discount_incomplete and _is_known_bad_gold(gold_sig):
            g_fp = 0
        genus_score = prf1(g_tp, g_fp, g_fn)

        pairs.append((gold_sig.source_type if gold_sig is not None else None, taxa_score, genus_score))

        if gold_sig is not None and pred_sig is not None:
            # A blank gold direction (~3% of gold signatures, `direction is
            # None`) carries no signal to check a prediction against --
            # excluded from the denominator entirely rather than counted as
            # an automatic miss (see this module's docstring / §4d).
            if gold_sig.direction is not None:
                direction_total += 1
                pred_direction = pred_sig.get("abundance_in_group_1")
                if pred_direction == gold_sig.direction:
                    direction_correct += 1

            gold_name_to_id = {resolver.id_to_name[t]: t for t in gold_ids if t in resolver.id_to_name}
            for taxon in pred_taxa_list:
                raw_name = taxon.get("taxon_name")
                if not raw_name:
                    continue
                norm = normalize_taxon_name(raw_name)
                if norm in gold_name_to_id:
                    name_to_id_found += 1
                    if resolver.resolve_taxon(taxon) == gold_name_to_id[norm]:
                        name_to_id_correct += 1

    return _SignatureScoreDetail(
        pairs=pairs,
        direction_correct=direction_correct,
        direction_total=direction_total,
        name_to_id_found=name_to_id_found,
        name_to_id_correct=name_to_id_correct,
        n_unresolved_pred_taxa=n_unresolved,
    )


# ---------------------------------------------------------------------------
# per-study / aggregate scoring
# ---------------------------------------------------------------------------


def score_study(
    gold: GoldStudy,
    predicted: dict[str, Any] | None,
    resolver: TaxonomyResolver,
    *,
    discount_incomplete: bool = True,
) -> StudyScore:
    """Score one study's prediction (or `None`/missing -> scored as empty) against gold."""
    pred_experiments: list[dict[str, Any]] = (predicted or {}).get("experiments", []) or []
    gold_experiments = list(gold.experiments)
    alignment = align_experiments(gold_experiments, pred_experiments, resolver=resolver)

    all_pairs: list[tuple[SourceType | None, PRF1, PRF1]] = []
    direction_correct = 0
    direction_total = 0
    name_to_id_found = 0
    name_to_id_correct = 0
    n_unresolved = 0

    for pred_idx, gold_idx in alignment.matched:
        detail = score_experiment_signatures(
            list(gold_experiments[gold_idx].signatures),
            pred_experiments[pred_idx].get("signatures", []) or [],
            resolver,
            discount_incomplete=discount_incomplete,
        )
        all_pairs.extend(detail.pairs)
        direction_correct += detail.direction_correct
        direction_total += detail.direction_total
        name_to_id_found += detail.name_to_id_found
        name_to_id_correct += detail.name_to_id_correct
        n_unresolved += detail.n_unresolved_pred_taxa

    # Unmatched gold experiments: every gold taxon is a full miss (FN only).
    for gold_idx in alignment.unmatched_gold:
        for sig in gold_experiments[gold_idx].signatures:
            genus = frozenset(g for g in (resolver.genus_of_id(i) for i in sig.taxa) if g)
            all_pairs.append((sig.source_type, prf1(0, 0, len(sig.taxa)), prf1(0, 0, len(genus))))

    # Unmatched predicted experiments: every predicted taxon is a full FP
    # (there's no gold signature here at all, so no source_type/discount applies).
    for pred_idx in alignment.unmatched_pred:
        for sig in pred_experiments[pred_idx].get("signatures", []) or []:
            ids, unresolved = _resolve_pred_taxa(sig.get("taxa", []) or [], resolver)
            n_unresolved += unresolved
            genus = frozenset(g for g in (resolver.genus_of_id(i) for i in ids) if g)
            all_pairs.append((None, prf1(0, len(ids), 0), prf1(0, len(genus), 0)))

    taxa_scores = [p for _, p, _ in all_pairs]
    genus_scores = [p for _, _, p in all_pairs]

    source_type_counts: dict[str, int] = {}
    for exp in gold_experiments:
        for sig in exp.signatures:
            source_type_counts[sig.source_type] = source_type_counts.get(sig.source_type, 0) + 1

    return StudyScore(
        study_id=gold.study_id,
        has_pmc=gold.has_pmc,
        n_gold_experiments=len(gold_experiments),
        n_pred_experiments=len(pred_experiments),
        experiment_alignment=alignment,
        micro_taxa=_micro(taxa_scores),
        macro_taxa_f1=_mean([p.f1 for p in taxa_scores]),
        micro_genus=_micro(genus_scores),
        macro_genus_f1=_mean([p.f1 for p in genus_scores]),
        direction_correct=direction_correct,
        direction_total=direction_total,
        name_to_id_correct=name_to_id_correct,
        name_to_id_found=name_to_id_found,
        n_unresolved_pred_taxa=n_unresolved,
        source_type_counts=source_type_counts,
        signature_pairs=tuple((st, p) for st, p, _ in all_pairs),
    )


def aggregate_scores(study_scores: list[StudyScore]) -> AggregateScore:
    """Corpus/smoke-set-level aggregate, stratified by source type and
    experiment-count bucket (§4c/§4e)."""
    all_pairs: list[tuple[str | None, PRF1]] = []
    for s in study_scores:
        all_pairs.extend(s.signature_pairs)

    by_source_type: dict[str, PRF1] = {}
    for st in ("main-table", "figure", "supplement", "other"):
        items = [p for tag, p in all_pairs if tag == st]
        if items:
            by_source_type[st] = _micro(items)

    by_bucket_items: dict[str, list[PRF1]] = {}
    for s in study_scores:
        bucket = experiment_count_bucket(s.n_gold_experiments)
        by_bucket_items.setdefault(bucket, []).extend(p for _, p in s.signature_pairs)
    by_experiment_bucket = {bucket: _micro(items) for bucket, items in by_bucket_items.items() if items}

    direction_correct = sum(s.direction_correct for s in study_scores)
    direction_total = sum(s.direction_total for s in study_scores)
    name_to_id_correct = sum(s.name_to_id_correct for s in study_scores)
    name_to_id_found = sum(s.name_to_id_found for s in study_scores)

    all_taxa_prf1 = [p for _, p in all_pairs]
    return AggregateScore(
        n_studies=len(study_scores),
        micro_taxa=_micro(all_taxa_prf1),
        macro_taxa_f1=_mean([p.f1 for p in all_taxa_prf1]),
        direction_accuracy=(direction_correct / direction_total) if direction_total else 0.0,
        name_to_id_accuracy=(name_to_id_correct / name_to_id_found) if name_to_id_found else 0.0,
        by_source_type=by_source_type,
        by_experiment_bucket=by_experiment_bucket,
        over_segmentation=sum(s.experiment_alignment.over_segmentation for s in study_scores),
        under_segmentation=sum(s.experiment_alignment.under_segmentation for s in study_scores),
    )
