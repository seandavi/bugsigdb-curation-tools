"""Unit tests for `bugsigdb_curation.retrieval`'s new pure parsers: body
`<sec>` sections, `<table-wrap>` tables, and `<article-meta>` bibliographic
fields -- the S1 evidence-assembly primitives the de-novo curator adds on top
of the figure-extraction benchmark's original figure/blob/OA parsers (which
are covered by `tests/test_figbench_retrieve.py` and re-exported verbatim by
`benchmarks/figure-extraction/retrieve.py`; see that module's docstring).

All tests use tiny inline XML fixtures -- no network access.
"""

from __future__ import annotations

from bugsigdb_curation.retrieval import (
    parse_article_metadata,
    parse_fulltext_sections,
    parse_fulltext_tables,
)

FULLTEXT_XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta>
      <journal-title-group><journal-title>Gut Microbes</journal-title></journal-title-group>
    </journal-meta>
    <article-meta>
      <article-id pub-id-type="doi">10.1234/example.2021</article-id>
      <title-group><article-title>A study of <italic>Bacteroides</italic> in CRC</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Smith</surname><given-names>Jane A</given-names></name></contrib>
        <contrib contrib-type="author"><name><surname>Doe</surname><given-names>John</given-names></name></contrib>
        <contrib contrib-type="editor"><name><surname>NotAnAuthor</surname><given-names>X</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2021</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="s1">
      <title>Methods</title>
      <p>We recruited 40 subjects.</p>
      <sec id="s1a">
        <title>Statistical analysis</title>
        <p>We used  LEfSe   with default parameters.</p>
      </sec>
    </sec>
    <sec id="s2">
      <title>Results</title>
      <p>Bacteroides was <italic>increased</italic> in cases.</p>
    </sec>
  </body>
</article>
"""

TABLE_XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <body>
    <table-wrap id="T2">
      <label>Table 2.</label>
      <caption><p>Differentially abundant taxa.</p></caption>
      <table>
        <thead><tr><th>Taxon</th><th>LDA score</th></tr></thead>
        <tbody>
          <tr><td>Bacteroides</td><td>3.2</td></tr>
          <tr><td>Prevotella</td><td>-2.9</td></tr>
        </tbody>
      </table>
    </table-wrap>
    <table-wrap>
      <caption><p>No label, no rows.</p></caption>
    </table-wrap>
  </body>
</article>
"""

NO_BODY_XML = "<article><front/></article>"


# --- parse_fulltext_sections -------------------------------------------------------------


def test_parse_fulltext_sections_extracts_title_and_own_text_only():
    sections = parse_fulltext_sections(FULLTEXT_XML_FIXTURE)
    assert [s.section_id for s in sections] == ["s1", "s1a", "s2"]

    methods = sections[0]
    assert methods.title == "Methods"
    # Own <p> text only -- the nested subsection's paragraph is NOT duplicated in here.
    assert methods.text == "We recruited 40 subjects."

    stats = sections[1]
    assert stats.title == "Statistical analysis"
    assert stats.text == "We used LEfSe with default parameters."

    results = sections[2]
    assert results.title == "Results"
    assert results.text == "Bacteroides was increased in cases."


def test_parse_fulltext_sections_returns_empty_list_when_no_body():
    assert parse_fulltext_sections(NO_BODY_XML) == []


def test_parse_fulltext_sections_synthesizes_id_when_missing():
    xml = """<article><body><sec><title>No id</title><p>text</p></sec></body></article>"""
    sections = parse_fulltext_sections(xml)
    assert sections[0].section_id == "sec-0"


# --- parse_fulltext_tables ---------------------------------------------------------------


def test_parse_fulltext_tables_extracts_label_caption_and_rows():
    tables = parse_fulltext_tables(TABLE_XML_FIXTURE)
    assert len(tables) == 2

    t2 = tables[0]
    assert t2.table_id == "T2"
    assert t2.label == "Table 2."
    assert t2.number == "2"
    assert t2.caption == "Differentially abundant taxa."
    assert t2.rows == (
        ("Taxon", "LDA score"),
        ("Bacteroides", "3.2"),
        ("Prevotella", "-2.9"),
    )


def test_parse_fulltext_tables_handles_missing_label_and_rows():
    tables = parse_fulltext_tables(TABLE_XML_FIXTURE)
    t_unlabeled = tables[1]
    assert t_unlabeled.label == ""
    assert t_unlabeled.number is None
    assert t_unlabeled.rows == ()
    assert t_unlabeled.table_id == "table-1"


# --- parse_article_metadata ---------------------------------------------------------------


def test_parse_article_metadata_extracts_bibliographic_fields():
    meta = parse_article_metadata(FULLTEXT_XML_FIXTURE)
    assert meta.title == "A study of Bacteroides in CRC"
    assert meta.journal == "Gut Microbes"
    assert meta.year == 2021
    assert meta.doi == "10.1234/example.2021"
    # Only contrib-type="author" entries are collected, editors excluded.
    assert meta.authors == ("Jane A Smith", "John Doe")


def test_parse_article_metadata_returns_all_none_when_no_article_meta():
    meta = parse_article_metadata(NO_BODY_XML)
    assert meta.title is None
    assert meta.journal is None
    assert meta.year is None
    assert meta.authors == ()
    assert meta.doi is None
