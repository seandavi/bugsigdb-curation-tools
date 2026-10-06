"""Control for ``figbench``: the same questions with the image withheld (legend text only).

If accuracy matches the with-image run, the model is answering from the legend
(or priors), not reading the figure.
"""

from __future__ import annotations

from typing import Any

from bugsigdb_curation.decision import DecisionModel
from experiments import figbench

score = figbench.score


class _NoImages:
    def __init__(self, inner: DecisionModel) -> None:
        self._inner = inner

    async def decide(self, *, stage: str, state: Any, questions: Any, images: Any = ()):
        return await self._inner.decide(stage=stage, state=state, questions=questions, images=())


async def run(model: DecisionModel) -> dict[str, Any]:
    return await figbench.run(_NoImages(model))
