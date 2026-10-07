"""P1/P4 on the 47 pages of the 34620922 supplementary PDF (image variant).

One page per call, rendered to JPEG at 100 dpi (``bugsigdb_curation.pdf``). Labels:
``labels/p1_34620922_pages.yaml`` (agent-drafted; see its header).
"""

from __future__ import annotations

from typing import Any

import yaml
from probe_common import LABELS, REPO

from bugsigdb_curation.decision import DecisionModel
from bugsigdb_curation.pdf import PdfDoc, open_pdf
from experiments import _screen

PDF = REPO / "data" / "decision-probe" / "34620922" / "supplements" / "41598_2021_99379_MOESM1_ESM.pdf"
DPI = 100
#: The Workers AI pre-flight token estimate scales with the *encoded* image size (a 0.8 MB PNG of a
#: photo page was estimated at 274k tokens vs a 65k context), so encode pages as JPEG and cap the bytes.
MAX_IMAGE_BYTES = 200_000


def render_page(doc: PdfDoc, index: int) -> bytes:
    """Page `index` (0-based) as a JPEG of at most :data:`MAX_IMAGE_BYTES` where quality 35 allows."""
    for quality in (80, 65, 50, 35):
        data = doc.render_jpeg(index, DPI, quality)
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    return data


def labels() -> dict[int, dict[str, Any]]:
    doc = yaml.safe_load((LABELS / "p1_34620922_pages.yaml").read_text())
    out = {}
    for page, lab in doc["pages"].items():
        lab = dict(lab)
        if lab.get("arity") == "two_group":
            lab["n_groups"] = 2
        elif lab.get("arity", "").startswith("multi"):
            lab["n_groups"] = 4 if page == 27 else 3
        out[int(page)] = lab
    return out


def units(*, with_image: bool = True, with_text: bool = False) -> list[dict[str, Any]]:
    labs = labels()
    result = []
    with open_pdf(PDF.read_bytes()) as doc:
        for i in range(1, doc.n_pages + 1):
            state: Any = {"file": PDF.name, "page": i}
            if with_text:
                state["page_text"] = doc.page_text(i - 1)[:12000]
            result.append(
                {
                    "id": f"p{i:02d}",
                    "state": state,
                    "images": [render_page(doc, i - 1)] if with_image else [],
                    "label": labs[i],
                }
            )
    return result


async def run(model: DecisionModel) -> dict[str, Any]:
    return await _screen.screen(model, units(), stage="supp_page")


score = _screen.score
