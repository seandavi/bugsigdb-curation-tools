"""P1/P4 on the 14 xlsx sheets of 37864204's supplements: header + first 30 rows as the state."""

from __future__ import annotations

from typing import Any

import openpyxl
import yaml
from probe_common import LABELS, REPO

from bugsigdb_curation.decision import DecisionModel
from experiments import _screen

SUPP = REPO / "data" / "decision-probe" / "37864204" / "supplements"
N_ROWS = 30


def units() -> list[dict[str, Any]]:
    labs = yaml.safe_load((LABELS / "p1_37864204_files.yaml").read_text())["sheets"]
    out = []
    for key, label in labs.items():
        moesm, sheet = key.split("::")
        path = next(SUPP.glob(f"*_{moesm}_ESM.xlsx"))
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        rows = [
            ["" if c is None else (f"{c:.4g}" if isinstance(c, float) else str(c))[:60] for c in row[:15]]
            for row in wb[sheet].iter_rows(values_only=True, max_row=N_ROWS)
        ]
        out.append({"id": key, "state": {"file": path.name, "sheet": sheet, "first_rows": rows}, "images": [], "label": label})
    return out


async def run(model: DecisionModel) -> dict[str, Any]:
    return await _screen.screen(model, units(), stage="supp_sheet")


score = _screen.score
