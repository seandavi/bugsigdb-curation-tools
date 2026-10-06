"""Reviewer verdicts on machine-generated BugSigDB drafts: schema, validation, ingest, report.

A review packet (`review.packet`) lets a BugSigDB curator judge one draft in a browser and export
one JSON file. This module owns everything that happens to that file afterwards:

* `validate_verdicts` -- check a file against `schema/review_verdict.schema.json` (schema_version 1);
* `ingest_verdict_files` -- validate, check the draft hash against the packet manifest, and file it
  under `DEST/<pmid>/<reviewer_slug>_<exported_at>.json`;
* `load_reviews` + `render_report` -- aggregate every ingested verdict into a markdown report.

Since the reviewed papers have no gold, these verdicts *are* the evaluation. A `null` verdict means
the reviewer left that item unreviewed; it is counted separately and never as a positive or negative.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_VERSION = 1

DEFAULT_REVIEWS_DIR = Path("data/reviews")

ITEM_VERDICTS = ("ok", "needs_edit", "wrong", "unsure")
DIRECTION_VERDICTS = ("ok", "flipped", "unsure")
TAXON_VERDICTS = ("correct", "wrong_taxon", "not_in_source", "unsure")


def canonical_sha256(record: Any) -> str:
    """SHA-256 of the canonical JSON of `record` (sorted keys, compact separators, non-ASCII kept).

    Raises ValueError for NaN/Infinity: they are not JSON, so a draft holding one cannot be reviewed.
    """
    try:
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"the draft holds NaN or Infinity, which is not valid JSON: {exc}") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_schema_path() -> Path:
    """Path to `review_verdict.schema.json`: packaged copy first, then the repo-root `schema/` (dev installs)."""
    resource = resources.files("bugsigdb_curation").joinpath("data", "review_verdict.schema.json")
    if resource.is_file():
        return Path(str(resource))
    dev_path = Path(__file__).resolve().parents[3] / "schema" / "review_verdict.schema.json"
    if dev_path.is_file():
        return dev_path
    raise FileNotFoundError(f"Could not locate review_verdict.schema.json (checked {resource} and {dev_path}).")


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(review_schema_path().read_text(encoding="utf-8")))


_PMID_RE = re.compile(r"[A-Za-z0-9_-]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})")


def parse_timestamp(text: str) -> datetime:
    """A verdict-file timestamp (ASCII ISO 8601 with `Z` or an offset) as an aware UTC datetime.

    Raises ValueError for anything else, including well-formed text that is not a real instant
    (`2026-02-30...`, hour 25) and non-ASCII digits.
    """
    if not _TIMESTAMP_RE.fullmatch(text):
        raise ValueError(f"not an ISO 8601 timestamp: {text!r}")
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"not a real timestamp: {text!r} ({exc})") from exc


def validate_verdicts(data: Any) -> list[str]:
    """Problems in one verdict file's parsed JSON, as `path: message` strings (empty = valid).

    The schema checks the shape; code then checks what its regexes cannot: `pmid` / `draft_sha256`
    must match in full (a `$` pattern accepts a trailing newline) and both timestamps must be real
    instants. Everything downstream (file names, ordering) may rely on these.
    """
    errors = sorted(_validator().iter_errors(data), key=lambda e: list(e.absolute_path))
    problems = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors]
    if problems:
        return problems
    for key, pattern in (("pmid", _PMID_RE), ("draft_sha256", _SHA256_RE)):
        if not pattern.fullmatch(data[key]):
            problems.append(f"{key}: {data[key]!r} does not match {pattern.pattern}")
    for key in ("started_at", "exported_at"):
        try:
            parse_timestamp(data[key])
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
    return problems


_MAX_SLUG_LENGTH = 64


def _slug(text: str) -> str:
    """Lower-cased letters and digits of any script, joined by `-`; never holds a path separator, `.` or NUL."""
    return re.sub(r"[^\w]+", "-", unicodedata.normalize("NFKC", text).lower(), flags=re.UNICODE).strip("-")


def _short_hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:8]


def reviewer_slug(reviewer: dict[str, Any]) -> str:
    """Filesystem-safe identity of a reviewer, at most 64 characters.

    The slugged name, else the slugged email, else `r-` + a hash of name and email (so reviewers whose
    names have no sluggable characters, e.g. emoji, still stay distinct); `anonymous` only when both are
    empty. Slugs keep non-ASCII letters: reviewers named 王伟 and 李娜 are two reviewers.
    """
    name, email = reviewer.get("name") or "", reviewer.get("email") or ""
    slug = _slug(name) or _slug(email)
    if not slug:
        return f"r-{_short_hash(name, email)}" if name or email else "anonymous"
    if len(slug) > _MAX_SLUG_LENGTH:
        slug = f"{slug[: _MAX_SLUG_LENGTH - 9].rstrip('-')}-{_short_hash(name, email)}"
    return slug


def _filename_timestamp(exported_at: str) -> str:
    """`2026-10-06T12:34:56.789+02:00` -> `20261006T103456Z` (UTC, no colons: portable file names)."""
    return parse_timestamp(exported_at).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of ingesting one verdict file. `status`: ingested | duplicate | invalid | refused | conflict."""

    source: Path
    status: str
    message: str
    dest: Path | None = None
    warnings: tuple[str, ...] = ()


def _find_manifest(verdict_path: Path, pmid: str, manifests_dir: Path | None) -> Path | None:
    for directory in (manifests_dir,) if manifests_dir is not None else (verdict_path.parent,):
        candidate = directory / f"{pmid}.manifest.json"
        if candidate.is_file():
            return candidate
    return None


def ingest_verdict_file(
    path: Path, *, dest: Path = DEFAULT_REVIEWS_DIR, manifests_dir: Path | None = None, force: bool = False
) -> IngestResult:
    """Validate one verdict file and copy it to `dest/<pmid>/<reviewer_slug>_<exported_at>.json`.

    A destination file that already exists with different content is a `conflict` (two reviews would
    otherwise silently replace each other) unless `force`; with identical content it is a `duplicate`
    and nothing is written. The packet manifest (`<pmid>.manifest.json`, looked up in `manifests_dir`, or beside the verdict
    file when none is given) pins the `draft_sha256` the reviewer was shown. A mismatch means the
    verdicts judge a different draft than the one on record: refused unless `force`, in which case
    it is ingested with a warning. No manifest found is not an error (the packet may be long gone),
    but is reported as a warning.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return IngestResult(path, "invalid", f"not readable as JSON: {exc}")

    errors = validate_verdicts(data)
    if errors:
        shown = "; ".join(errors[:5]) + (f"; ... ({len(errors)} violations)" if len(errors) > 5 else "")
        return IngestResult(path, "invalid", f"schema violations: {shown}")

    warnings: list[str] = []
    manifest_path = _find_manifest(path, data["pmid"], manifests_dir)
    if manifest_path is None:
        warnings.append(f"no manifest for PMID {data['pmid']}; draft_sha256 not checked")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return IngestResult(path, "invalid", f"manifest {manifest_path} unreadable: {exc}")
        if manifest.get("draft_sha256") != data["draft_sha256"]:
            message = (
                f"draft_sha256 {data['draft_sha256'][:12]} does not match manifest {manifest_path.name} "
                f"({str(manifest.get('draft_sha256'))[:12]}): verdicts judge a different draft"
            )
            if not force:
                return IngestResult(path, "refused", message + " (use --force to ingest anyway)")
            warnings.append(message)

    target = dest / data["pmid"] / f"{reviewer_slug(data['reviewer'])}_{_filename_timestamp(data['exported_at'])}.json"
    if target.exists():
        if target.read_bytes() == path.read_bytes():
            return IngestResult(path, "duplicate", f"already ingested as {target}", dest=target, warnings=tuple(warnings))
        if not force:
            message = f"{target} already holds a different review by this reviewer from the same second"
            return IngestResult(path, "conflict", message + " (use --force to overwrite it)")
        warnings.append(f"overwrote {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    return IngestResult(path, "ingested", f"-> {target}", dest=target, warnings=tuple(warnings))


def ingest_verdict_files(
    paths: Iterable[Path], *, dest: Path = DEFAULT_REVIEWS_DIR, manifests_dir: Path | None = None, force: bool = False
) -> list[IngestResult]:
    return [ingest_verdict_file(p, dest=dest, manifests_dir=manifests_dir, force=force) for p in paths]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StudyTally:
    """Pooled verdict counts for one draft (one `pmid` + `draft_sha256`) across its reviewers."""

    pmid: str
    draft_sha256: str
    reviewers: set[str] = field(default_factory=set)
    study: Counter[str] = field(default_factory=Counter)
    experiments: Counter[str] = field(default_factory=Counter)
    taxa: Counter[str] = field(default_factory=Counter)
    directions: Counter[str] = field(default_factory=Counter)
    signature_directions: dict[tuple[int, int], Counter[str]] = field(default_factory=dict)
    ratings: list[int] = field(default_factory=list)
    would_publish: Counter[str] = field(default_factory=Counter)
    minutes: list[float] = field(default_factory=list)
    notes: list[tuple[str, str, str]] = field(default_factory=list)  # (reviewer, scope, text)


def taxa_precision(taxa: Counter[str]) -> float | None:
    """correct / (correct + wrong_taxon + not_in_source); `unsure` and unreviewed excluded. None if nothing judged."""
    judged = taxa["correct"] + taxa["wrong_taxon"] + taxa["not_in_source"]
    return taxa["correct"] / judged if judged else None


def flip_rate(directions: Counter[str]) -> float | None:
    """flipped / (ok + flipped); `unsure` excluded. None if nothing judged."""
    judged = directions["ok"] + directions["flipped"]
    return directions["flipped"] / judged if judged else None


def ok_rate(verdicts: Counter[str]) -> float | None:
    """ok / (ok + needs_edit + wrong); `unsure` excluded. None if nothing judged."""
    judged = verdicts["ok"] + verdicts["needs_edit"] + verdicts["wrong"]
    return verdicts["ok"] / judged if judged else None


def _tally_one(tally: StudyTally, v: dict[str, Any]) -> None:
    who = reviewer_slug(v["reviewer"])
    tally.reviewers.add(who)
    tally.study[v["study"]["verdict"] or "unreviewed"] += 1
    if v["study"]["note"].strip():
        tally.notes.append((who, "study note", v["study"]["note"].strip()))
    for exp in v["experiments"]:
        tally.experiments[exp["verdict"] or "unreviewed"] += 1
        if exp["missing_note"].strip():
            tally.notes.append((who, f"experiment {exp['index'] + 1} missing from draft", exp["missing_note"].strip()))
        for sig in exp["signatures"]:
            direction = sig["direction_verdict"] or "unreviewed"
            tally.directions[direction] += 1
            tally.signature_directions.setdefault((exp["index"], sig["index"]), Counter())[direction] += 1
            for taxon in sig["taxa"]:
                tally.taxa[taxon["verdict"] or "unreviewed"] += 1
    if v["missing_experiments_note"].strip():
        tally.notes.append((who, "missing experiments", v["missing_experiments_note"].strip()))
    overall = v["overall"]
    if overall["time_saved_rating"] is not None:
        tally.ratings.append(overall["time_saved_rating"])
    if overall["would_publish_after_edits"] is not None:
        tally.would_publish[overall["would_publish_after_edits"]] += 1
    if overall["comment"].strip():
        tally.notes.append((who, "overall comment", overall["comment"].strip()))
    if v["minutes_spent"] is not None:
        tally.minutes.append(float(v["minutes_spent"]))


def tally_verdicts(verdicts: Sequence[dict[str, Any]]) -> list[StudyTally]:
    """Group validated verdict files by draft (`pmid`, `draft_sha256`) and pool their counts.

    A reviewer who exported the same draft more than once counts once: only their latest export
    (by `exported_at`, compared as instants) is used, so re-exports after further edits supersede earlier ones.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for v in verdicts:
        key = (v["pmid"], v["draft_sha256"], reviewer_slug(v["reviewer"]))
        if key not in latest or parse_timestamp(v["exported_at"]) >= parse_timestamp(latest[key]["exported_at"]):
            latest[key] = v
    tallies: dict[tuple[str, str], StudyTally] = {}
    for (pmid, sha, _), v in sorted(latest.items()):
        _tally_one(tallies.setdefault((pmid, sha), StudyTally(pmid, sha)), v)
    return list(tallies.values())


def load_reviews(reviews_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every valid verdict file under `reviews_dir` (recursive) plus `path: problem` strings for the rest."""
    verdicts: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in sorted(reviews_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: not readable as JSON: {exc}")
            continue
        errors = validate_verdicts(data)
        if errors:
            problems.append(f"{path}: {errors[0]}")
        else:
            verdicts.append(data)
    return verdicts, problems


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ratio(num: int, den: int) -> str:
    return f"{num}/{den}"


def _mean(values: Sequence[float]) -> str:
    return f"{statistics.fmean(values):.2f}" if values else "n/a"


def _taxa_cell(taxa: Counter[str]) -> str:
    judged = taxa["correct"] + taxa["wrong_taxon"] + taxa["not_in_source"]
    return f"{_pct(taxa_precision(taxa))} ({_ratio(taxa['correct'], judged)})"


def _sig_cell(directions: Counter[str]) -> str:
    judged = directions["ok"] + directions["flipped"]
    return f"{_pct(flip_rate(directions))} ({_ratio(directions['flipped'], judged)})"


def _exp_cell(experiments: Counter[str]) -> str:
    return f"{experiments['ok']} / {experiments['needs_edit']} / {experiments['wrong']}"


def _merge(tallies: Sequence[StudyTally]) -> StudyTally:
    total = StudyTally("ALL", "")
    for t in tallies:
        total.reviewers |= t.reviewers
        total.study += t.study
        total.experiments += t.experiments
        total.taxa += t.taxa
        total.directions += t.directions
        total.ratings += t.ratings
        total.would_publish += t.would_publish
        total.minutes += t.minutes
    return total


LEGEND = """\
## Legend

- **Taxa precision** = correct / (correct + wrong taxon + not in source). Taxa marked *unsure* or left
  unreviewed are excluded from the denominator and reported separately.
- **Direction flip rate** = flipped / (ok + flipped) over signature direction verdicts; *unsure* excluded.
- **Experiments ok / needs edit / wrong** = number of experiment verdicts of each kind (counted per
  reviewer); *unsure* and unreviewed are listed separately.
- **Study ok rate** = ok / (ok + needs edit + wrong) over study-level verdicts; *unsure* excluded.
- **Time saved** = reviewers' 1-5 rating of "compared with curating from scratch, this draft would save me
  ...": 1 = no time, 5 = most of the time. Mean over reviewers who gave a rating.
- Counts pool every reviewer of the same draft. A reviewer who exported the same draft more than once counts
  once (latest export). Reviews of different versions of a draft (different `draft_sha256`) are reported
  as separate rows.
- No gold exists for these papers: these numbers are reviewer judgements, not a comparison with curated data.
"""


def render_report(tallies: Sequence[StudyTally], *, problems: Sequence[str] = ()) -> str:
    """Markdown report for pooled tallies: overall, per study, direction detail, and free-text notes."""
    total = _merge(tallies)
    shas_per_pmid = Counter(t.pmid for t in tallies)

    def label(t: StudyTally) -> str:
        return f"{t.pmid} (draft {t.draft_sha256[:8]})" if shas_per_pmid[t.pmid] > 1 else t.pmid

    lines = ["# BugSigDB review report", ""]
    lines += LEGEND.splitlines() + [""]
    if problems:
        lines += [f"**Skipped {len(problems)} unreadable or invalid verdict file(s):**", ""]
        lines += [f"- {p}" for p in problems] + [""]
    if not tallies:
        return "\n".join(lines + ["No verdicts found.", ""])

    unsure_taxa, unreviewed_taxa = total.taxa["unsure"], total.taxa["unreviewed"]
    study_judged = total.study["ok"] + total.study["needs_edit"] + total.study["wrong"]
    publish_cell = " / ".join(str(total.would_publish[k]) for k in ("yes", "no", "unsure"))
    lines += [
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Studies (drafts) reviewed | {len(tallies)} |",
        f"| Reviewers | {len(total.reviewers)} |",
        f"| Taxa precision | {_taxa_cell(total.taxa)} |",
        f"| Taxa unsure / unreviewed | {unsure_taxa} / {unreviewed_taxa} |",
        f"| Direction flip rate | {_sig_cell(total.directions)} |",
        f"| Direction unsure / unreviewed | {total.directions['unsure']} / {total.directions['unreviewed']} |",
        f"| Experiments ok / needs edit / wrong | {_exp_cell(total.experiments)} |",
        f"| Experiments unsure / unreviewed | {total.experiments['unsure']} / {total.experiments['unreviewed']} |",
        f"| Study ok rate | {_pct(ok_rate(total.study))} ({_ratio(total.study['ok'], study_judged)}) |",
        f"| Mean time-saved rating (1-5) | {_mean(total.ratings)} (n={len(total.ratings)}) |",
        f"| Would publish after edits yes / no / unsure | {publish_cell} |",
        f"| Mean minutes spent | {_mean(total.minutes)} (n={len(total.minutes)}) |",
        "",
        "## Per study",
        "",
        (
            "| PMID | Reviewers | Taxa precision | Taxa unsure | Direction flip rate "
            "| Experiments ok / needs edit / wrong | Study ok rate | Mean time saved |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in tallies:
        lines.append(
            f"| {label(t)} | {len(t.reviewers)} | {_taxa_cell(t.taxa)} | {t.taxa['unsure']} | "
            f"{_sig_cell(t.directions)} | {_exp_cell(t.experiments)} | {_pct(ok_rate(t.study))} | "
            f"{_mean(t.ratings)} |"
        )

    lines += [
        "",
        "## Direction verdicts per signature",
        "",
        "| PMID | Experiment | Signature | ok | flipped | unsure | Flip rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in tallies:
        for (e, s), d in sorted(t.signature_directions.items()):
            lines.append(
                f"| {label(t)} | {e + 1} | {s + 1} | {d['ok']} | {d['flipped']} | {d['unsure']} | {_pct(flip_rate(d))} |"
            )

    lines += ["", "## Reviewer notes (missing from the draft, and comments)", ""]
    any_notes = False
    for t in tallies:
        if t.notes:
            any_notes = True
            lines.append(f"### {label(t)}")
            lines.append("")
            for who, scope, text in t.notes:
                lines.append(f"- **{who}** ({scope}): {' '.join(text.split())}")
            lines.append("")
    if not any_notes:
        lines += ["No free-text notes.", ""]
    return "\n".join(lines)
