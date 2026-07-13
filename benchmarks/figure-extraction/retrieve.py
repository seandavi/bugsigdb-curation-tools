"""Retrieval helpers for the figure-extraction benchmark.

This module used to contain the retrieval recipe directly; it has since been
consolidated into :mod:`bugsigdb_curation.retrieval` so the de-novo curator
(`bugsigdb_curation.curator.evidence`, S1 evidence assembly) can share the
same pure parsers and fetch wrappers rather than duplicating them. Everything
below is a verbatim re-export, so ``import retrieve`` + ``retrieve.<name>``
keeps working exactly as before for this benchmark's scripts and tests (see
``README.md`` for the retrieval recipe writeup and ``tests/test_figbench_retrieve.py``).
"""

from __future__ import annotations

from bugsigdb_curation.retrieval import (  # noqa: F401
    BROWSER_USER_AGENT,
    EUROPEPMC_FULLTEXT_URL,
    OA_SERVICE_URL,
    PMC_ARTICLE_URL,
    FigureEntry,
    build_image_path,
    extract_blob_urls,
    fetch_article_html,
    fetch_fulltext_xml,
    fetch_image_bytes,
    fetch_oa_status,
    match_figure,
    match_filename_to_blob,
    normalize_source_label,
    parse_fulltext_figures,
    parse_oa_response,
)
