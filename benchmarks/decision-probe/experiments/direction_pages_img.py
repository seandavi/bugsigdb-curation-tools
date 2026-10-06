"""``direction_pages`` with the rendered page image instead of extracted text (layout-preserving)."""

from __future__ import annotations

from typing import Any

from bugsigdb_curation.decision import DecisionModel
from experiments import direction_pages

score = direction_pages.score


async def run(model: DecisionModel) -> dict[str, Any]:
    return await direction_pages.run(model, with_image=True)
