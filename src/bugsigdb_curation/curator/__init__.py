"""De-novo BugSigDB curator: Design-1 (Fused-Lean), linear single-worker form.

Turns a bare PMID into a schema-valid `Study -> experiments[] -> signatures[]
-> taxa[]` prediction record (the shape `bugsigdb_curation.loader` emits and
`bugsigdb_curation.eval.score` consumes), per
`docs/plans/de-novo-curation-workflow-plan.md` §6.

**Data firewall (§6e, non-negotiable):** this package and everything under it
must never import `bugsigdb_curation.eval` (or any gold loader) and must
never read a gold CSV. See `tests/test_curator_firewall.py` for the guard
test that enforces this at CI time. The curator's public entrypoint,
`bugsigdb_curation.curator.pipeline.curate`, takes a PMID (+ model/config)
and nothing else -- no gold path of any kind.
"""

from __future__ import annotations
