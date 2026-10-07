# BugSigDB Curation Schema (LinkML)

[![CI](https://github.com/seandavi/bugsigdb-curation-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/bugsigdb-curation-tools/actions/workflows/ci.yml)

A [LinkML](https://linkml.io) representation of the [BugSigDB](https://bugsigdb.org)
curation data model, reverse-engineered from the Semantic MediaWiki + Page Forms
application that curators use. This is the foundation for an automated curation agent
that extracts published microbial signatures from papers.

BugSigDB captures **microbial signatures**: sets of microbial taxa reported as
differentially abundant between two groups of samples in a published study.

## Layout

| Path | Contents |
|------|----------|
| `schema/bugsigdb.yaml` | The LinkML schema. 6 classes, 63 slots, 12 controlled-vocabulary enums. |
| `sources/` | Local snapshot of the wiki schema pages it was derived from (see below). |

### `sources/` — the source material

A faithful scrape of the curation schema as encoded at bugsigdb.org (the site is
behind Cloudflare, so pages were pulled via the MediaWiki API at `/w/api.php`):

- `forms/` — Page Forms definitions (input types, mandatory flags, defaults, conditional display).
- `templates/` — how form fields map to stored semantic properties.
- `properties/` — one file per SMW property; datatypes and the tooltip text shown to curators.
- `values/` — snapshots of the controlled-vocabulary value lists (countries, host species, body sites, statistical tests, …).
- `help/` — human curation guidance pages.

## Data model

A three-level hierarchy, annotated with controlled vocabularies and ontology terms:

```
Study            one publication (identified by `uid`, the wiki page name; usually a PMID)
└── Experiment   one two-group comparison (Group 0 = control, Group 1 = case)
    └── Signature  taxa changing in one direction (increased/decreased in Group 1)
```

Plus `Taxon` (NCBI Taxonomy nodes), `Review` (review workflow), and a
`CurationProvenance` mixin. Ontology bindings: condition → EFO, body site → UBERON,
host species and signature taxa → NCBI Taxonomy.

Every class, slot, and enum carries `description` plus dual-audience `comments`
(`CURATOR:` for humans, `AGENT:` for the automated extractor).

## Validate / generate

```bash
uvx --from linkml gen-json-schema schema/bugsigdb.yaml   # -> JSON Schema
uvx --from linkml gen-owl         schema/bugsigdb.yaml   # -> OWL
uvx --from linkml gen-pydantic    schema/bugsigdb.yaml   # -> Pydantic models
```

## CLI

`bugsigdb export` downloads the generated export artifacts (merged CSV dump
and/or GMT signature sets) from the [`waldronlab/bugsigdbexports`](https://github.com/waldronlab/bugsigdbexports)
repo:

```bash
uv run bugsigdb export --list              # see what's available, no download
uv run bugsigdb export                     # full_dump.csv + file_size.csv -> data/exports/
uv run bugsigdb export --select gmt        # GMT signature sets instead
uv run bugsigdb export --select all        # everything
uv run bugsigdb export --ref v1.2.3        # a specific tag/branch instead of devel
uv run bugsigdb export --force             # re-download even if a same-size file exists
```

Existing files are skipped when their size already matches the remote (use
`--force` to override). Downloads stream to disk with bounded concurrency and
a `rich` progress bar; run `uv run bugsigdb export --help` for all options.

`bugsigdb validate` checks one or more curated instance files (YAML or JSON,
each holding a single object or a list of objects) against the LinkML schema,
using the [`linkml` validator](https://linkml.io/linkml/schemas/validation.html):

```bash
uv run bugsigdb validate study.yaml                          # validate as a Study (default)
uv run bugsigdb validate study.yaml other.yaml                # multiple files in one invocation
uv run bugsigdb validate experiment.yaml -C Experiment         # validate against a different class
uv run bugsigdb validate study.yaml --schema my-schema.yaml    # override the schema
uv run bugsigdb validate study.yaml --format json              # machine-readable report
```

Exit codes: `0` if every instance is valid, `1` if any instance fails schema
validation (bad enum value, wrong type, missing required field, …), `2` for
usage/IO errors (file not found, unparseable YAML/JSON, unknown
`--target-class`, bad `--schema` path). Run `uv run bugsigdb validate --help`
for all options.

`bugsigdb load` parses a `full_dump.csv` export (denormalized: one row per
Signature, with Study/Experiment columns repeated) into nested
Study -> Experiment -> Signature records matching `schema/bugsigdb.yaml`'s
slot names:

```bash
uv run bugsigdb load data/exports/full_dump.csv                     # -> YAML on stdout
uv run bugsigdb load data/exports/full_dump.csv --format json       # JSON instead
uv run bugsigdb load data/exports/full_dump.csv -o studies.yaml      # to a file
uv run bugsigdb load data/exports/full_dump.csv --limit 20           # first 20 studies only
```

Studies are keyed by `uid` (the stable `Study` page name, always present);
`pmid` is captured separately as an optional integer when present. Cells are
coerced to the schema's types (ints, floats, bools,
enum values) and multivalued cells are split into lists; blank ("NA") cells
are simply omitted rather than defaulted or invented. `MetaPhlAn taxon names`
and `NCBI Taxonomy IDs` are paired up into `Taxon` records (id + name + rank
+ lineage). The output is plain nested dicts (no `linkml`/`pydantic`
dependency) so it can later be checked structurally, e.g. by a `bugsigdb
validate` command. See `bugsigdb_curation/loader.py` for the exact
column -> slot mapping and delimiter conventions (derived from inspecting the
real dump, not just the wiki docs — some columns disagree with the wiki's
documented delimiters).

`bugsigdb pmc-map` maps curated studies' PMIDs to PubMed Central IDs
(PMCIDs) via the [NCBI PMC ID Converter API](https://www.ncbi.nlm.nih.gov/pmc/tools/id-converter-api/),
producing a gold/eval set for de-novo curation workflows (which typically
need PMC full text, not just a PubMed abstract):

```bash
uv run bugsigdb pmc-map                                  # data/exports/relational/studies.csv -> data/eval/pmid_pmcid_map.csv
uv run bugsigdb pmc-map --input studies.csv --output map.csv
uv run bugsigdb pmc-map --email me@example.org            # NCBI etiquette for unauthenticated use
uv run bugsigdb pmc-map --limit 50                        # first 50 distinct PMIDs only, for testing
```

Reads distinct numeric PMIDs from a relational `studies.csv` (as produced by
`bugsigdb split`; the 16 or so studies with no PMID at all are skipped),
queries idconv in batches of 200 with bounded (3-way) concurrency, and
writes `study_id,pmid,pmcid,doi,has_pmc` — one row per study, so studies
that share a PMID both appear. A coverage summary (`N PMIDs: M with PMCID
(X%), K without.`) is printed to stderr. Of the current 2068 curated
studies, 2052 have a numeric PMID, of which about 84% resolve to a PMCID.

`--limit` truncates the *distinct PMID* list before querying, not the study
rows — so any study row whose PMID falls outside that truncated subset is
excluded from the output CSV (its PMID was simply never queried). When that
happens, a `Note: N study row(s) excluded (PMID outside --limit).` line is
printed to stderr so the row-count drop isn't silent.

### Human review packets (`bugsigdb review`)

Drafts written by `bugsigdb curate` have no gold, so BugSigDB curators' verdicts
are the evaluation. A **review packet** is one self-contained `.html` file (inline
CSS/JS, no network requests): the reviewer opens it, judges the draft against the
paper, clicks *Export*, and sends back one JSON file.

```bash
# 1. build a packet (+ <pmid>.manifest.json pinning the draft's sha256)
uv run bugsigdb review packet --pred preds/21850056.json --out packets/ \
    --model-label gemini-3-pro --design-label split-verify
#    picks up preds/21850056.annotations.json automatically; --offline skips fetching evidence,
#    --evidence-dir DIR caches it (complete fetches only; --refresh-evidence refetches),
#    --pmcid overrides the PMID->PMCID lookup
# 2. combine a directory of packets into ONE shareable bundle (index.html + packets + docs) and zip it
uv run bugsigdb review bundle --packets packets/ --out share/ --contact "Jane Doe <jane@example.org>"
#    -> share/bugsigdb-review-<date>/ and share/bugsigdb-review-<date>.zip; send the zip to named reviewers
# 3. file returned verdicts (validated against schema/review_verdict.schema.json)
uv run bugsigdb review ingest ~/Downloads/verdicts_21850056_*.json --manifests packets/   # -> data/reviews/<pmid>/
# 4. aggregate
uv run bugsigdb review report --reviews data/reviews --out report.md
```

Figure images are embedded only when the article's EuropePMC licence is CC BY or
CC0 (each image under ~1.5 MB, all images together under ~6 MB); otherwise the packet
shows the legend and a link. `ingest` refuses verdicts whose `draft_sha256` differs
from the manifest, and refuses to overwrite a different file already filed for the
same reviewer and second, unless `--force`; re-ingesting an identical file is a no-op. Verdicts contain reviewer names/emails: `data/` is git-ignored.

#### Sharing packets: `bugsigdb review bundle`

Reviewers (BugSigDB curators) have no accounts, so the recommended way to share is
one zip sent to named reviewers. `review bundle --packets DIR --out DIR2` turns a
directory of packets into a folder `DIR2/<name>/` (and, by default, `DIR2/<name>.zip`)
holding:

- `index.html` — a self-contained landing page (inline CSS, no JavaScript, no external requests): a
  "machine-generated, unreviewed" notice, how to review, and one card per study (title, authors,
  journal/year, PMID/PMCID/DOI links, licence, experiment/signature/taxon counts, the figures and
  tables the draft cites, an *Open packet →* link, file size, packet id);
- `packets/<pmid>.html` and `packets/<pmid>.manifest.json` — the packets, copied byte for byte;
- `README.txt` (the how-to and the verdict legend, for people who read the zip listing first),
  `ATTRIBUTION.txt` (authors, journal, DOI and licence per study; flags packets whose licence is not
  CC BY/CC0 or whose images were not embedded) and `manifest.json` (every file's sha256 and size, the
  packets' ids and draft hashes, the build date, the builder commit when the packets agree on it, and `content_sha256` — a hash of
  the packets that the index footer also shows, so two people can confirm they hold the same bundle).

Options: `--name` (default `bugsigdb-review-<date>`), `--contact "name <email>"` (where reviewers send
their verdict files; shown in the index and README), `--zip/--no-zip` (default `--zip`), `--date YYYY-MM-DD`
(default today; recorded in the manifest — the only clock reading). The build is offline and
deterministic: the same packets, name, date and contact give a byte-identical zip (sorted members,
fixed 2000-01-01 timestamps and permissions). It refuses (exit 2) a packet without a manifest, with a
`packet_id` or `draft_sha256` that does not match its embedded record, a duplicate PMID, an empty
directory, or an existing `<name>/` folder; non-CC-BY or image-less packets only warn. It never
modifies the packets.

Caveats: some institutional mail gateways strip or quarantine zips that contain `.html`/`.js` — if a
reviewer does not receive it, fall back to a shared drive / Box link or a private GitHub release
asset. Reviewers must **extract the zip before opening** a packet (not from a zip preview or an
attachment viewer), or the page's script will not run.

The loop: build packets → `review bundle` → send the zip → reviewers export verdict JSON and send it
back → `review ingest` → `review report`.

## License

Schema released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
consistent with BugSigDB.
