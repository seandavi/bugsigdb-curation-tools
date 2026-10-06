"""P5: ontology assignment as a ``choice`` over deterministically retrieved candidates.

Unit: each distinct single-term gold ``body_site`` (UBERON) / ``condition``
(EFO/MONDO/HP/...) label, with the owning study's title as context.

Candidates come from **OLS4 search over the ontology** (query = the label text), never from
gold frequencies. The gold term may not be among the top 10: that is reported as
retrieval recall@10, separately from choice accuracy given the gold term is present.

Caveat (issue #19): gold labels are already normalized vocabulary ("Feces"), so retrieval is far
easier than for production S4 free text.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
from collections import defaultdict
from typing import Any

import httpx
from probe_common import RELATIONAL, REPO, bounded_gather, coverage_at_confidence

from bugsigdb_curation.decision import Choice, DecisionModel

OLS_URL = "https://www.ebi.ac.uk/ols4/api/search"
CACHE = REPO / "data" / "decision-probe" / "ols"
ONTOLOGIES = {"body_site": "uberon", "condition": "efo,mondo,hp"}
NONE_OPTION = "none_of_these"
ROWS = 10
FIELD_COLS = {"body_site": "uberon_id", "condition": "efo_id"}


def gold_units(field: str) -> list[dict[str, Any]]:
    """One unit per distinct single-term label with exactly one id; with usage counts."""
    id_col = FIELD_COLS[field]
    titles = {r["study_id"]: r["title"] for r in csv.DictReader((RELATIONAL / "studies.csv").open())}
    by_label: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for row in csv.DictReader((RELATIONAL / "experiments.csv").open()):
        label, ident = row[field].strip(), row[id_col].strip()
        if not label or not ident or "," in label or "," in ident:
            continue
        unit = by_label.setdefault(label, {"label": label, "gold_id": ident, "n_experiments": 0, "study_title": titles.get(row["study_id"], "")})
        if unit["gold_id"] != ident:
            ambiguous.add(label)
        unit["n_experiments"] += 1
        if not unit["study_title"]:
            unit["study_title"] = titles.get(row["study_id"], "")
    return sorted((u for l, u in by_label.items() if l not in ambiguous), key=lambda u: u["label"])


async def search_ols(client: httpx.AsyncClient, query: str, ontology: str) -> list[dict[str, Any]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{ontology}|{query}|{ROWS}".encode()).hexdigest()
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    for attempt in range(4):
        r = await client.get(OLS_URL, params={"q": query, "ontology": ontology, "rows": ROWS, "type": "class", "queryFields": "label,synonym,short_form,obo_id"})
        if r.status_code == 200:
            docs = r.json()["response"]["docs"]
            path.write_text(json.dumps(docs))
            return docs
        await asyncio.sleep(2 ** attempt)
    r.raise_for_status()
    return []


def describe(doc: dict[str, Any]) -> dict[str, Any]:
    syn = (doc.get("exact_synonyms") or [])[:5]
    definition = (doc.get("description") or [""])[0][:300]
    out: dict[str, Any] = {"label": doc.get("label", "")}
    if definition:
        out["definition"] = definition
    if syn:
        out["synonyms"] = syn
    return out


async def run_field(model: DecisionModel, field: str, limit: int | None = None) -> dict[str, Any]:
    units = gold_units(field)[:limit]
    async with httpx.AsyncClient(timeout=60) as ols:
        sem_ols = asyncio.Semaphore(5)

        async def retrieve(u: dict[str, Any]) -> list[dict[str, Any]]:
            async with sem_ols:
                return await search_ols(ols, u["label"], ONTOLOGIES[field])

        candidates = await asyncio.gather(*(retrieve(u) for u in units))

    async def decide(pair: tuple[dict[str, Any], list[dict[str, Any]]]) -> dict[str, Any]:
        u, docs = pair
        ids = [d["obo_id"] for d in docs if d.get("obo_id")]
        crit: dict[str, Any] = {d["obo_id"]: describe(d) for d in docs if d.get("obo_id")}
        crit[NONE_OPTION] = "none of the candidate terms is a good match"
        res: dict[str, Any] = {
            "label": u["label"], "gold_id": u["gold_id"], "n_experiments": u["n_experiments"],
            "candidates": ids, "gold_in_candidates": u["gold_id"] in ids,
        }
        if len(crit) < 2:
            return res | {"choice": None, "confidence": None}
        ans = (await model.decide(
            stage=f"ontology_{field}",
            state={"field": field, "label": u["label"], "study_title": u["study_title"]},
            questions={"term": Choice({"question": f"Which ontology term does this {field.replace('_', ' ')} label refer to?", "label": u["label"], "study_title": u["study_title"]}, crit)},
        ))["term"]
        return res | {"choice": ans.choice, "confidence": ans.confidence}  # type: ignore[union-attr]

    return {"field": field, "units": await bounded_gather(list(zip(units, candidates)), decide, limit=8)}


def score_field(results: dict[str, Any]) -> dict[str, Any]:
    units = results["units"]
    n = len(units)
    present = [u for u in units if u["gold_in_candidates"] and u["choice"] is not None]
    absent = [u for u in units if not u["gold_in_candidates"] and u["choice"] is not None]
    ok = [u["choice"] == u["gold_id"] for u in present]
    conf = [u["confidence"] for u in present]
    w = sum(u["n_experiments"] for u in present) or 1
    prefix_recall: dict[str, list[bool]] = defaultdict(list)
    for u in units:
        prefix_recall[u["gold_id"].split(":")[0]].append(u["gold_in_candidates"])
    return {
        "n_units": n,
        "retrieval_recall_at_10": sum(u["gold_in_candidates"] for u in units) / n,
        "retrieval_recall_by_ontology": {k: {"n": len(v), "recall": sum(v) / len(v)} for k, v in sorted(prefix_recall.items())},
        "choice_accuracy_given_present": sum(ok) / len(ok) if ok else None,
        "choice_accuracy_weighted_by_experiments": sum(u["n_experiments"] for u, o in zip(present, ok) if o) / w,
        "n_present": len(present),
        "none_of_these_rate_when_absent": (sum(u["choice"] == NONE_OPTION for u in absent) / len(absent)) if absent else None,
        "n_absent": len(absent),
        "end_to_end_accuracy": (sum(o for o in ok) / n),
        "coverage_at_confidence": coverage_at_confidence(ok, conf, (0.0, 0.5, 0.7, 0.9, 0.95)),
        "errors": [{"label": u["label"], "gold": u["gold_id"], "picked": u["choice"], "conf": round(u["confidence"], 3)} for u, o in zip(present, ok) if not o][:25],
    }
