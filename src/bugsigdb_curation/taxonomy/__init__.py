"""A local, offline, DuckDB-backed NCBI taxonomy resolver built from the taxdump.

Standalone from `bugsigdb_curation.curator`/`bugsigdb_curation.eval` -- this
subpackage only ever reads the general NCBI taxdump (authoritative, not gold;
workflow plan §6e) and never imports the eval package (nor `curator`/`eval`
import anything from each other through it). PR-2 wires this local DB into
both consumers as a fast local-first lookup ahead of their existing live
paths: `curator.taxonomy.NcbiTaxonomyResolver` (cache -> this DB -> live
E-utilities gap-fill -> unresolved) and `eval.taxonomy.TaxonomyResolver`
(cache -> this DB -> unresolved, replacing its old `taxa.csv`-derived seed).

Public surface:

- :func:`bugsigdb_curation.taxonomy.build.build_taxonomy_db` -- build a
  `.duckdb` file from a local NCBI taxdump (`names.dmp` + `nodes.dmp`, or a
  `taxdump.tar.gz`/`.zip` archive containing them).
- :class:`bugsigdb_curation.taxonomy.db.TaxonomyDB` -- open a built `.duckdb`
  read-only and resolve names / walk lineages.
- :mod:`bugsigdb_curation.taxonomy.paths` -- cache/DB path resolution
  (CLI flag > env var > XDG-cache default), shared by the CLI here and,
  later, by the curator/scorer.
"""

from __future__ import annotations
