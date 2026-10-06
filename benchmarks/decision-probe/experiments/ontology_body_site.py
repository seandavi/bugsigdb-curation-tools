"""P5 ontology assignment for the gold `body_site` labels (see `ontology.py`)."""

from __future__ import annotations

from typing import Any

from bugsigdb_curation.decision import DecisionModel
from experiments import ontology

score = ontology.score_field


async def run(model: DecisionModel) -> dict[str, Any]:
    return await ontology.run_field(model, "body_site")
