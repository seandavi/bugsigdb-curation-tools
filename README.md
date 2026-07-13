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
Study            one publication (page name = PMID)
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

## License

Schema released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
consistent with BugSigDB.
