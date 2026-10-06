"""P1/P4 on the same 47 pages, but the page's extracted text is the state (no image)."""

from __future__ import annotations

from typing import Any

from bugsigdb_curation.decision import DecisionModel
from experiments import _screen, supp_pages

score = _screen.score


async def run(model: DecisionModel) -> dict[str, Any]:
    return await _screen.screen(model, supp_pages.units(with_image=False, with_text=True), stage="supp_page_txt")
