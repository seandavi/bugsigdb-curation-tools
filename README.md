# BugSigDB Curation Schema (LinkML)

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

## License

Schema released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
consistent with BugSigDB.
