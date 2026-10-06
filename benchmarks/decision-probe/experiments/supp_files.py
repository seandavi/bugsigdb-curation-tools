"""P1 pre-download screening: each supplementary *file* of 37864204 judged from its manifest entry alone.

State is ``{filename, caption, media_type}`` -- what ``supplements.parse_supplement_refs`` gives us before
any bytes are fetched. Labels: ``labels/p1_37864204_files.yaml`` (agent-drafted).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from bugsigdb_curation.decision import DecisionModel
from bugsigdb_curation.supplements import _media_type_for_filename
from common import LABELS, REPO
from experiments import _screen


def units() -> list[dict[str, Any]]:
    labs = yaml.safe_load((LABELS / "p1_37864204_files.yaml").read_text())["refs"]
    bundle = json.loads((REPO / "data" / "decision-probe" / "37864204" / "bundle.json").read_text())
    seen: set[str] = set()
    out = []
    for ref in bundle["supplement_refs"]:
        name = ref["filename"]
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "id": name.split("_")[-2],
                "state": {"filename": name, "media_type": _media_type_for_filename(name), "label": ref["label"], "caption": ref["caption"][:1500]},
                "images": [],
                "label": labs[name],
            }
        )
    return out


async def run(model: DecisionModel) -> dict[str, Any]:
    return await _screen.screen(model, units(), stage="supp_file")


score = _screen.score
