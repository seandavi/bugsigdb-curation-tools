"""`Design` -- the curator's stage-design selector (workflow plan §6a/§6b).

Three designs, a diagonal across two stage-design axes over a FIXED backbone
(S0-S4 + S5a locate + S8 assemble + S9 validate, unchanged by any of this):

- **A1 -- NER<->reconcile split** (S5b/S6): `fused-lean` keeps one model call
  that both extracts taxa+direction AND proposes the `ncbi_id` (tool-
  verified only, see `curator.signature`). `split-verify`/`split-panel` both
  use the **split** form instead: `curator.ner` (names+direction only, no id
  proposal) -> `curator.reconcile` (deterministic `TaxonomyDB` resolution,
  disambiguation-on-ambiguous-hit only) -- ids come only from the authority.
- **A2 -- reviewer/validator stage** (S10): `fused-lean` is structural-only
  (S9's schema/CURIE validation, no semantic re-derivation). `split-verify`
  adds `curator.verify`'s adversarial verifier (taxon-in-source + direction
  re-derivation, bounded repair). `split-panel` adds `curator.panel`'s
  independent reviewer + arbitration (agree/reconcile/recall, bounded
  repair).

`curator.pipeline.curate_async` dispatches S5b/S6 and S10 on this selector;
every other stage module (S0-S4, S5a, S8, S9) is identical across all three
designs -- see that module's per-stub loop.
"""

from __future__ import annotations

from enum import Enum


class Design(str, Enum):
    """One of the three designs from the workflow plan's §6b table.

    A `str` subclass so a plain string (`"fused-lean"`, ...) compares equal
    to and interoperates with the enum member everywhere a caller might pass
    either -- CLI flags, test fixtures, and `CurationResult.design`'s stored
    value all use the same three literal strings.
    """

    fused_lean = "fused-lean"
    split_verify = "split-verify"
    split_panel = "split-panel"


#: `fused-lean` is the existing walking skeleton -- the default so every
#: caller/test that doesn't pass `--design`/`design=` explicitly keeps
#: today's behavior unchanged.
DEFAULT_DESIGN = Design.fused_lean

#: Every valid `--design` value, in the §6b table's order (cheapest ->
#: most-verified).
ALL_DESIGNS: tuple[Design, ...] = (Design.fused_lean, Design.split_verify, Design.split_panel)
