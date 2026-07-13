"""Score blind figure-extraction predictions against the BugSigDB gold.

Compares each ``predictions/<pmcid>.json`` (produced by a vision extractor that
never saw the gold) with the gold taxa/directions in ``manifest.json``.

Metrics per figure and aggregated by figure type:
  * taxa set precision / recall / F1 -- STRICT (exact normalized taxon name)
    and GENUS-LENIENT (collapse to genus token, so a genus-level prediction
    matches a species-level gold of the same genus and vice versa).
  * direction accuracy over strictly-matched taxa (fraction whose predicted
    up/down matches gold; predictions of "unknown" count as wrong but are
    also reported separately).

Taxon names are normalized by stripping MetaPhlAn-style rank prefixes
(``g__``, ``s__`` ...), lowercasing, and collapsing whitespace/underscores.
Known limitation: this is string-based, so genuine NCBI synonyms
(e.g. Propionibacterium vs Cutibacterium) will not match -- noted in the report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Rank prefixes appear as double underscore (MetaPhlAn, "g__Bacillus") or single
# underscore (LEfSe figure labels, "g_Bacillus"); strip either form.
_RANK_PREFIX = re.compile(r"^[kdpcofgst]__?")


def normalize(name: str) -> str:
    """Normalize a taxon label for exact matching."""
    n = name.strip()
    n = _RANK_PREFIX.sub("", n)
    n = n.replace("_", " ")
    n = re.sub(r"\s+", " ", n)
    return n.strip().lower()


def genus_of(norm_name: str) -> str:
    """Genus token of an already-normalized name (first word)."""
    return norm_name.split(" ")[0] if norm_name else ""


@dataclass
class FigureScore:
    pmcid: str
    figure_type: str
    n_gold: int
    n_pred: int
    tp_strict: int
    tp_genus: int
    dir_matched: int
    dir_correct: int
    dir_unknown: int

    def prf(self, tp: int) -> tuple[float, float, float]:
        p = tp / self.n_pred if self.n_pred else 0.0
        r = tp / self.n_gold if self.n_gold else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return p, r, f


def _gold_direction_map(entry: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for sig in entry["gold"]:
        for t in sig["taxa"]:
            out[normalize(t["name"])] = sig["direction"]
    return out


def _pred_direction_map(pred: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for grp in pred.get("predicted", []):
        for name in grp.get("taxa", []):
            out[normalize(name)] = grp.get("direction", "unknown")
    return out


def score_entry(entry: dict, pred: dict) -> FigureScore:
    gold = _gold_direction_map(entry)
    predm = _pred_direction_map(pred)
    gold_names, pred_names = set(gold), set(predm)

    tp_strict = gold_names & pred_names
    gold_genus = {genus_of(n) for n in gold_names}
    pred_genus = {genus_of(n) for n in pred_names}
    tp_genus = gold_genus & pred_genus

    dir_matched = dir_correct = dir_unknown = 0
    for name in tp_strict:
        pd = predm[name]
        if pd == "unknown":
            dir_unknown += 1
            continue
        dir_matched += 1
        if pd == gold[name]:
            dir_correct += 1

    return FigureScore(
        pmcid=entry["pmcid"],
        figure_type=entry["figure_type"],
        n_gold=len(gold_names),
        n_pred=len(pred_names),
        tp_strict=len(tp_strict),
        tp_genus=len(tp_genus),
        dir_matched=dir_matched,
        dir_correct=dir_correct,
        dir_unknown=dir_unknown,
    )


def score_all(manifest_path: Path, predictions_dir: Path) -> list[FigureScore]:
    manifest = {e["pmcid"]: e for e in json.loads(Path(manifest_path).read_text())}
    scores: list[FigureScore] = []
    for pmcid, entry in manifest.items():
        pfile = Path(predictions_dir) / f"{pmcid}.json"
        if not pfile.exists():
            continue
        pred = json.loads(pfile.read_text())
        scores.append(score_entry(entry, pred))
    return scores


def _fmt(scores: list[FigureScore]) -> str:
    lines = [
        "| PMCID | figure type | gold | pred | strict P/R/F1 | genus F1 | dir acc |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for s in sorted(scores, key=lambda x: x.figure_type):
        sp, sr, sf = s.prf(s.tp_strict)
        _, _, gf = s.prf(s.tp_genus)
        dacc = f"{s.dir_correct}/{s.dir_matched}" + (f" (+{s.dir_unknown} unk)" if s.dir_unknown else "")
        lines.append(
            f"| {s.pmcid} | {s.figure_type} | {s.n_gold} | {s.n_pred} | "
            f"{sp:.2f}/{sr:.2f}/{sf:.2f} | {gf:.2f} | {dacc} |"
        )
    # aggregate by type
    lines += ["", "### By figure type (micro-averaged)", "", "| figure type | n | strict F1 | genus F1 | dir acc |", "|---|---:|---:|---:|---:|"]
    by_type: dict[str, list[FigureScore]] = {}
    for s in scores:
        by_type.setdefault(s.figure_type, []).append(s)
    for ftype, group in sorted(by_type.items()):
        tg = sum(s.n_gold for s in group)
        tp = sum(s.n_pred for s in group)
        tps = sum(s.tp_strict for s in group)
        tpg = sum(s.tp_genus for s in group)
        dm = sum(s.dir_matched for s in group)
        dc = sum(s.dir_correct for s in group)
        ps = tps / tp if tp else 0.0
        rs = tps / tg if tg else 0.0
        fs = 2 * ps * rs / (ps + rs) if (ps + rs) else 0.0
        pg = tpg / tp if tp else 0.0  # genus precision uses raw pred count (upper bound)
        fg_r = tpg / tg if tg else 0.0
        fg = 2 * pg * fg_r / (pg + fg_r) if (pg + fg_r) else 0.0
        lines.append(f"| {ftype} | {len(group)} | {fs:.2f} | {fg:.2f} | {dc}/{dm} ({dc/dm*100:.0f}%) |" if dm else f"| {ftype} | {len(group)} | {fs:.2f} | {fg:.2f} | - |")
    return "\n".join(lines)


if __name__ == "__main__":
    base = Path(__file__).parent
    scores = score_all(base / "manifest.json", base / "predictions")
    report = _fmt(scores)
    print(report)
    # machine-readable
    (base / "results.json").write_text(
        json.dumps([s.__dict__ for s in scores], indent=1)
    )
