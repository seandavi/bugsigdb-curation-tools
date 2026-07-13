"""S3 -- experiment segmentation: bundle -> a list of 2-group comparison stubs.

One model call over the assembled text proposes how many distinct
2-group comparisons the paper reports and a short natural-language
description of each -- the "experiment scaffold" that S4/S5 then each
fill in, one stub at a time (Architecture A's simple loop; see the plan §2).
"""

from __future__ import annotations

from dataclasses import dataclass

from bugsigdb_curation.curator.evidence import EvidenceBundle
from bugsigdb_curation.curator.model import Model, build_text_content

_SEGMENT_TEXT_CHARS = 8000


@dataclass(frozen=True, slots=True)
class ExperimentStub:
    """One proposed 2-group comparison, not yet filled in with §1b fields."""

    index: int
    description: str


def build_segment_messages(bundle: EvidenceBundle) -> list[dict]:
    prompt = (
        "You are segmenting a microbiome research paper into its distinct 2-group comparisons "
        "for BugSigDB curation. Each comparison is a pair of subject groups (e.g. cases vs. "
        "controls, before vs. after treatment, one body site vs. another) whose microbiome "
        "composition the paper compares.\n\n"
        "List every distinct comparison the paper reports, in the order it's most naturally "
        "described. A paper with only one comparison should return a list of exactly one item.\n\n"
        'Return ONLY a JSON object: {"experiments": [{"index": 0, "description": "<one-line '
        'summary of the comparison, e.g. groups + body site + method>"}, ...]}\n\n'
        f"Article text:\n{bundle.full_text()[:_SEGMENT_TEXT_CHARS]}"
    )
    return [{"role": "user", "content": [build_text_content(prompt)]}]


def segment_experiments(bundle: EvidenceBundle, *, model: Model) -> list[ExperimentStub]:
    """S3: one model call proposing the experiment scaffold."""
    response = model.complete(stage="segment", messages=build_segment_messages(bundle))
    raw = response.get("experiments", []) or []

    stubs: list[ExperimentStub] = []
    for fallback_index, item in enumerate(raw):
        if isinstance(item, dict):
            index = item.get("index")
            index = int(index) if isinstance(index, (int, float)) else fallback_index
            description = str(item.get("description") or "")
        else:
            index, description = fallback_index, str(item)
        stubs.append(ExperimentStub(index=index, description=description))
    return stubs
