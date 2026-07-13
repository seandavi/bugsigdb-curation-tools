# Ontology Integration Plan — BugSigDB LinkML Schema

**Status:** Feasibility assessment + phased plan (planning only — no schema/loader/validator changes made)
**Author:** architecture review, 2026-07-13
**Scope:** Integrate ontology concept identifiers and terms into the BugSigDB LinkML schema (`schema/bugsigdb.yaml`) and produce *validated* ontology mappings of curated studies / experiments / signatures.

---

## 0. Terminology: "CUIs" — resolve before Phase 1

The request mentioned **"CUIs."** There are two readable meanings, and they imply very different work:

| Interpretation | What it means | Fit with BugSigDB |
| --- | --- | --- |
| **CURIE** (recommended) | Compact URI: `prefix:local`, e.g. `UBERON:0000995`, `EFO:0000305`, `NCBITaxon:562` | **Exactly what BugSigDB already stores.** The export's `UBERON ID` / `EFO ID` columns are 100% CURIE-formatted. No new crosswalk needed. |
| **UMLS CUI** | Unified Medical Language System concept id, e.g. `C0009402` | **Not present anywhere in BugSigDB.** Would require a UMLS licence + a crosswalk (EFO/MONDO/UBERON → UMLS CUI via MRCONSO/OxO/UMLS API). Large added effort, licensing constraints, and partial coverage. |

**Recommendation:** proceed on the **CURIE** interpretation. It matches the source data, the existing schema prefixes, and OBO/EBI tooling. **If Sean actually wants UMLS CUIs**, that becomes a separate downstream enrichment (map each CURIE → UMLS CUI via OxO / UMLS Metathesaurus), gated on a UMLS licence, and should be its own project after CURIE integration lands. This plan assumes CURIEs; a UMLS appendix is noted in §5.

---

## 1. Feasibility — grounded in the real export

All numbers below were measured from `data/exports/relational/*.csv` (split from the 29 MB `full_dump.csv`, snapshot `2026-07-13`).

**Corpus size:** 2,068 studies · 8,689 experiments · 14,156 signatures · 9,274 distinct taxa · 110,187 signature↔taxon links.

### 1a. Already mappable "for free" (IDs present in the dump)

The flat dump already carries three ontology-ID columns. Coverage is **excellent**:

| Field | Label column coverage | ID column | ID coverage | ID format | Distinct terms | Multi-valued? |
| --- | --- | --- | --- | --- | --- | --- |
| `body_site` | 8,645/8,689 (**99.5%**) | `UBERON ID` | 8,645/8,689 (**99.5%**) | 100% CURIE (`UBERON:0000995`) | 232 | up to 8 per exp (270 exps multi) |
| `condition` | 8,487/8,689 (**97.7%**) | `EFO ID` | 8,486/8,689 (**97.7%**) | 100% CURIE | 936 | up to 4 per exp (180 exps multi) |
| Signature `taxa` (`ncbi_id`) | — | `NCBI Taxonomy IDs` | 9,274/9,274 taxa, **0 missing** across all 110,187 links | **bare integer** (`820`, not `NCBITaxon:820`) | 9,274 | inherently multi |

Key facts:
- **Body site is essentially solved.** Every experiment with a body-site label also has a UBERON CURIE; **0** labels lack an ID; **0** IDs lack a label; label-count == ID-count in every row. UBERON IDs are *uniformly* prefixed `UBERON:`.
- **Condition IDs are multi-ontology, not "EFO only."** The column is named `EFO ID` but its contents span **15+ ontologies**: MONDO (4,204), EFO (3,231), HP (380), GO (223), CHEBI (181), OBA (144), NCBITaxon (78), IDOMAL (74), XCO (37), ENVO (30), CL (23), EXO (19), GSSO (16), PATO (14), OBI (11). This is expected — EFO imports terms from many OBO ontologies — but it means the schema must treat "condition id" as a **general CURIE across a set of allowed prefixes**, not a single-ontology binding.
- **Taxa are the cleanest field**: 100% of taxa carry an integer NCBI id; nothing is missing. Only gap vs. CURIE form: they are stored as **bare integers**, so emitting `NCBITaxon:820` is a pure formatting step.

### 1b. Needs new mapping work (no ID exists today)

| Field | Coverage / distinct | Ontology column in dump? | Gap |
| --- | --- | --- | --- |
| `host_species` | 0 blank, 79 distinct (`Homo sapiens` 6,798; `Mus musculus` 933; sentinel `Not specified` ×44) | **None** | Organismal NCBITaxon *names* only. No IDs stored. Small closed-ish list → cheap to map by hand/lookup (one-time ~79-row table). |
| `location_of_subjects` | 8 blank, 255 distinct label strings | **None** | Country names ≈ ISO-3166. No GAZ/ISO code stored. Would need a name→GAZ or name→ISO-3166 crosswalk (one-time, ~200 rows). |
| `matched_on` / `confounders_controlled_for` | shared ~289-value open list | **None** | Free-ish vocabulary, partly EFO-like (age, sex, BMI…). Mapping is *possible* but noisy; lowest priority. |
| Statistical/sequencing/design enums | closed enums already | n/a | Could get `meaning:` CURIEs (OBI/EDAM/STATO) as annotation, but no instance-level mapping needed — these are closed vocabularies, not curated free text. |

### 1c. The gap between "the dump gives us IDs" and "we can trust them"

Having a CURIE in a column is **not** the same as having a *validated* mapping:

1. **Schema prefix gap.** The dump uses 12 prefixes the schema does **not** declare in its `prefixes:` block: `CHEBI, CL, ENVO, EXO, GO, GSSO, HP, IDOMAL, OBA, OBI, PATO, XCO`. A `uriorcurie`-typed slot would fail CURIE→URI expansion for these until the prefix map is extended.
2. **No resolution / obsolescence check.** Nothing today verifies a CURIE actually exists in the current ontology or is not obsolete/merged.
3. **No branch check.** Nothing verifies a `body_site` id is really a UBERON anatomy term (vs. a stray MONDO id), or that a `condition` id sits under a plausible EFO/disease branch.
4. **No label↔id consistency check.** The curated label and the ontology's own `rdfs:label` for that CURIE are never compared, so drift (relabelled or re-scoped terms) is invisible.
5. **Label↔id positional pairing is fragile for condition.** UBERON: label-count == id-count in 100% of rows → safe positional zip. Condition: some labels contain a comma+space (`Hepatitis, Alcoholic`, `Genital neoplasm, female`), so naive comma splitting mis-counts. The loader's existing `split_comma_strict` (comma **not** followed by whitespace) already handles this correctly for labels — **the emitter must reuse it and must not assume the ID column uses the same delimiter** (IDs are `;`-or-`,`-joined). Pairing needs care and a per-row length assertion.

**Bottom line:** ~98–99% of body-site and condition annotations, and ~100% of taxa, are *mappable today* with near-zero curation effort. The real project is (a) plumbing the existing IDs through loader→schema as first-class CURIE slots, and (b) building the **validation** layer that turns "an ID is present" into "the ID is well-formed, resolvable, in the right ontology, and label-consistent." host_species / location are separate, smaller net-new mapping tasks.

---

## 2. Schema design options

### 2a. LinkML mechanisms available

1. **Permissible-value `meaning:`** — attach a CURIE to each value of a *closed* enum:
   ```yaml
   SequencingTypeEnum:
     permissible_values:
       16S: { meaning: OBI:0002763 }   # illustrative
   ```
   Good for the small closed enums (sequencing type/platform, data transformation, study design, alpha-diversity direction). Zero instance-data change; pure metadata enrichment. **Not** applicable to the open ontology fields (body_site/condition have hundreds of values and grow).

2. **Dynamic / `reachable_from` enums** — bind an enum to an ontology branch:
   ```yaml
   BodySiteEnum:
     reachable_from:
       source_ontology: obo:uberon
       source_nodes: [UBERON:0000061]   # anatomical structure
       relationship_types: [rdfs:subClassOf]
   ```
   Conceptually the "right" model, **but** materializing/validating a dynamic enum in LinkML requires the `vskit` / OAK toolchain and network or a bundled ontology; plain `linkml-validate` (JSON-Schema plugin, as used in `validate.py`) does **not** enforce `reachable_from`. Treat this as a *documented binding + external validator*, not something the current JSON-Schema validation path checks. Feasible but adds an OAK dependency and an ontology-version pin. Recommended as a **later** enhancement, not Phase 1.

3. **Reusable `OntologyTerm` class** — one class carrying id + label + source:
   ```yaml
   OntologyTerm:
     slots: [id, label, source_ontology]
     slot_usage:
       id: { range: uriorcurie, identifier: true }
   ```
   Most expressive (keeps label + CURIE + provenance together, supports mapping predicates), but it **restructures instance data**: `body_site` goes from `["feces"]` to `[{id: UBERON:0000995, label: feces}]`. Heavier migration; better if we expect rich mapping metadata (predicate, confidence, mapping source) per term.

4. **Paired `*_id` slots alongside existing label slots** — keep `body_site` (label) and add `body_site_id` (CURIE), `condition` + `condition_id`, `host_species` + `host_species_id`; emit `taxa[].ncbi_id` as-is (optionally add a CURIE view).
   - Lowest-friction: existing label slots and all downstream consumers keep working; IDs are additive and independently optional.
   - Matches the dump's own shape (parallel label + id columns) → trivial loader change.
   - Cost: label↔id alignment is by array position (must be enforced), and it doesn't bundle provenance.

5. **Supporting metaslots / types** (apply under any option above):
   - `range: uriorcurie` (or `curie`) on id slots so LinkML knows they are identifiers, enabling CURIE→URI expansion via the `prefixes:` map.
   - `pattern: "^[A-Za-z0-9_]+:[A-Za-z0-9_]+$"` for cheap well-formedness in the JSON-Schema path (catches empty/garbled CURIEs even without ontology resolution).
   - `id_prefixes:` on the id slot to declare the allowed prefix set (`[UBERON]` for body site; `[EFO, MONDO, HP, GO, CHEBI, OBA, NCBITaxon, IDOMAL, ...]` for condition) — documents intent and supports OAK-based branch checks later.
   - `exact_mappings:` / `mappings:` metaslots to record that, e.g., `condition_id` corresponds to EFO's factor axis.
   - **Extend `prefixes:`** to cover the 12 missing prefixes (§1c.1) so `uriorcurie` expansion succeeds for every observed CURIE.

### 2b. Recommendation (target model)

**Adopt Option 4 (paired `*_id` slots) as the Phase-1 target, typed `uriorcurie` with a `pattern` and `id_prefixes`, and pre-extend the `prefixes:` map.** It is additive (no breakage of existing label consumers), mirrors the export's native shape (cheap, low-risk loader change), and captures ~99% of the available mappings immediately. Emit taxa as bare `ncbi_id` (unchanged) with an optional derived `NCBITaxon:` CURIE view. Layer **Option 1 (`meaning:`)** onto the closed enums opportunistically — it's free metadata. Keep **Option 3 (`OntologyTerm`)** and **Option 2 (dynamic enums)** on the roadmap for when per-term provenance/confidence or branch-enforced validation become requirements; both are strictly richer supersets we can migrate to without discarding Phase-1 work. This sequences risk: ship the high-value 99% with a small diff, defer the heavier restructuring until it earns its keep.

### 2c. Sketch — after Phase 1

```yaml
prefixes:
  # ...existing... plus the 12 currently-missing ones:
  HP: http://purl.obolibrary.org/obo/HP_
  GO: http://purl.obolibrary.org/obo/GO_
  CHEBI: http://purl.obolibrary.org/obo/CHEBI_
  OBA: http://purl.obolibrary.org/obo/OBA_
  # CL, ENVO, EXO, GSSO, IDOMAL, OBI, PATO, XCO ...

slots:
  body_site:            # unchanged label slot (existing consumers keep working)
    multivalued: true
  body_site_id:
    description: UBERON CURIE(s) for the body site(s), positionally aligned with body_site.
    multivalued: true
    range: uriorcurie
    id_prefixes: [UBERON]
    pattern: "^UBERON:[0-9]{7}$"
    exact_mappings: [UBERON]

  condition:
    multivalued: true
  condition_id:
    description: Ontology CURIE(s) for the condition(s), aligned with condition. EFO-axis, but spans MONDO/HP/GO/CHEBI/... as EFO imports them.
    multivalued: true
    range: uriorcurie
    id_prefixes: [EFO, MONDO, HP, GO, CHEBI, OBA, NCBITaxon, IDOMAL, XCO, ENVO, CL, EXO, GSSO, PATO, OBI, DOID, ORPHANET, NCIT]
    pattern: "^[A-Za-z][A-Za-z0-9]*:[0-9]+$"

  host_species_id:      # net-new mapping (no ID in dump); optional, populated by a lookup table
    range: uriorcurie
    id_prefixes: [NCBITaxon]
    pattern: "^NCBITaxon:[0-9]+$"

# taxa: Taxon.ncbi_id stays integer (canonical key). Optional derived CURIE:
  Taxon:
    slot_usage:
      ncbi_id: { identifier: true }   # unchanged
    # optional: add `ncbitaxon_curie` (uriorcurie) derived as NCBITaxon:{ncbi_id}
```

---

## 3. Validated mappings of studies / experiments / signatures

Goal: turn "a CURIE is present" into a **validated** annotation. Four checks, in increasing cost:

| # | Check | Method | Cost |
| --- | --- | --- | --- |
| V1 | **Well-formed** CURIE (`prefix:local`, prefix known) | `pattern` + `id_prefixes` in schema; pure offline, runs in existing JSON-Schema path | ~free |
| V2 | **Right ontology / prefix set** (body_site ⊂ {UBERON}; condition ⊂ allowed set; host ⊂ {NCBITaxon}) | prefix-set membership check per slot | ~free, offline |
| V3 | **Resolves & not obsolete** (CURIE exists in current ontology, not deprecated/merged) | OAK against a **bundled ontology subset** (offline) *or* OLS4 / EBI OxO API (online) | moderate |
| V4 | **Label consistency** (curated label ≈ ontology `rdfs:label`/synonym for that CURIE) | OAK/OLS lookup of the term's label + fuzzy/synonym compare to curated label | moderate |

### 3a. Offline vs. online

- **Offline (recommended default): OAK (`oaklib`) over bundled, version-pinned ontology subsets.**
  - Build small SQLite/semsql extracts (or use OAK's cached adapters) for UBERON, EFO (with imports), NCBITaxon-organismal — or download the pinned `.db` once and cache under a `data/ontologies/` (git-ignored) or CI cache.
  - **Reproducible** (pinned version), **no network at validation time**, **CI-cheap** after first cache warm. Handles V3 (existence/obsolete) and V4 (labels/synonyms).
  - Cost: a one-time build/download step + a dependency (`oaklib`). NCBITaxon full is large (~2M nodes); prefer a taxon-slim or resolve taxa via a local NCBI names dump rather than full semsql.
- **Online: OLS4 (`https://www.ebi.ac.uk/ols4/api`) or EBI OxO.**
  - Zero local storage, always-current, easy to prototype.
  - **Not reproducible** (ontology drifts under you), rate-limited, flaky in CI, and privacy/availability coupling. Good for an interactive `enrich`/spot-check mode, **not** for gating CI.

**Recommendation:** V1/V2 always-on in `validate` (offline, free). V3/V4 via **OAK + pinned subsets**, with a `--online` OLS4 fallback for ad-hoc use. Cache ontology artifacts; pin versions in a manifest so results are reproducible and diffable.

### 3b. Where it lives

- **V1/V2** → fold into `bugsigdb validate` (they're just schema constraints once the `pattern`/`id_prefixes` land; the existing `JsonschemaValidationPlugin` path in `validate.py` enforces `pattern` automatically).
- **V3/V4** (ontology-aware, needs OAK/network) → **new command `bugsigdb map` (or `enrich`)** and/or a **new validation report**, kept *separate* from `validate` so the core validate stays dependency-light and offline. Two sub-modes:
  - `bugsigdb map --check` → validate existing CURIEs (V3/V4), emit a report (see §3c). Read-only.
  - `bugsigdb map --annotate` → *produce* mappings for fields lacking IDs (host_species, location) by looking up labels, writing a candidate mapping table for human review (never silently overwriting curated data).
- **Coverage/QC report** → a `bugsigdb map --report` (or `coverage`) that prints the §1 tables for any current export: % with id, distinct terms, prefix histogram, obsolete-term list, label-drift list. Useful both as a dashboard and as a CI artifact.

### 3c. Report shape (per run, machine + human readable)

```
field        n_annotated  n_with_id  %id    n_wellformed  n_resolved  n_obsolete  n_label_mismatch
body_site    8689         8645       99.5   8645          8643        2           17
condition    8689         8486       97.7   8486          8470        16          41
taxa         110187       110187     100    110187        110150      37          0
```
Obsolete + label-mismatch rows get an itemized appendix (CURIE, curated label, ontology label, suggested replacement) for curator follow-up.

---

## 4. Phased plan

Each phase is independently shippable. Effort: **S** ≈ ≤1 day, **M** ≈ 2–4 days, **L** ≈ ≥1 week.

### Phase 0 — Decisions + prefix hygiene (S)
- Resolve CURIE-vs-UMLS (§0) and the three §5 decisions.
- Extend `schema/bugsigdb.yaml` `prefixes:` with the 12 missing prefixes.
- **Files:** `schema/bugsigdb.yaml` (prefixes only).
- **Dependency:** none. Ships alone; unblocks everything.

### Phase 1 — Schema: add CURIE slots (S–M)
- Add `body_site_id`, `condition_id` (multivalued `uriorcurie`, `pattern`, `id_prefixes`); optionally `host_species_id`; wire them onto `Experiment`. Optionally add `meaning:` CURIEs to closed enums (free, can defer).
- Taxa: keep `ncbi_id` integer; optionally add derived `NCBITaxon:` view.
- **Files:** `schema/bugsigdb.yaml`. Regenerate any derived artifacts if present.
- **Dependency:** Phase 0. **Ships without loader changes** (new slots simply stay empty until Phase 2).

### Phase 2 — Loader emits CURIEs (M)
- In `loader._extract_experiment`, read `UBERON ID` / `EFO ID` columns (currently **dropped**) and emit `body_site_id` / `condition_id`, **positionally aligned** with the existing labels. Reuse `split_comma_strict` for labels; parse the id column with its own delimiter; assert per-row `len(labels) == len(ids)` and fall back safely (id-only, or flag) on mismatch — mirror the existing `parse_taxa` mismatch pattern.
- Optionally emit `taxa[].ncbitaxon_curie` (format `NCBITaxon:{ncbi_id}`).
- host_species_id: only if a lookup table exists (Phase 5); otherwise leave empty.
- **Files:** `src/bugsigdb_curation/loader.py` (+ tests). Possibly `cli.py` `load` if surfaced.
- **Dependency:** Phase 1. After this, a loaded export carries validated-shape CURIEs; `bugsigdb validate` enforces V1/V2 automatically.

### Phase 3 — Validation of mappings V1/V2 (S)
- Confirm `pattern`/`id_prefixes` are enforced by the existing JSON-Schema path; add targeted tests (bad prefix, malformed CURIE, wrong-ontology id in body_site). No new deps.
- **Files:** `src/bugsigdb_curation/validate.py` (likely no change), tests.
- **Dependency:** Phases 1–2. Ships as "structural CURIE validation."

### Phase 4 — Ontology-aware validation V3/V4 + `bugsigdb map` (L)
- New module (e.g. `src/bugsigdb_curation/mapping.py`) + `bugsigdb map` command. Integrate OAK; build/download pinned ontology subsets with a version manifest and local cache; implement resolve/obsolete (V3) and label-consistency (V4) checks; emit the §3c report. Add `--online` OLS4 fallback.
- **Files:** new `mapping.py`, `cli.py` (new command), `pyproject.toml` (add `oaklib`), CI cache config, tests.
- **Dependency:** Phases 1–2. Largest phase; can itself ship incrementally (V3 first, V4 next, `--annotate` last).

### Phase 5 — Net-new mappings: host_species, location (M)
- One-time curated crosswalks: `host_species` label → NCBITaxon CURIE (~79 rows), `location_of_subjects` → GAZ/ISO-3166 (~200 rows). Store as small versioned lookup tables (CSV/YAML under `data/` or `schema/`); loader consults them to populate `host_species_id` (+ optional `location_id`).
- **Files:** new lookup table(s), `loader.py`, `schema/bugsigdb.yaml` (add slots), tests.
- **Dependency:** Phase 1 (slots). Independent of Phase 4. `matched_on`/`confounders` mapping is explicitly **out of scope / lowest priority** (noisy, low value).

### Phase 6 — Dynamic-enum binding + `meaning:` polish (M, optional)
- If branch-enforced validation is wanted, add `reachable_from` bindings and drive them via OAK in `bugsigdb map` (the JSON-Schema path won't enforce them). Finish `meaning:` CURIEs on closed enums.
- **Dependency:** Phase 4 (OAK already integrated). Optional / roadmap.

**Incremental shipping:** Phases 0→1→2→3 deliver first-class, structurally-validated CURIEs for ~99% of body-site/condition and 100% of taxa with a *small* diff and no new heavy dependencies. Phase 4 (ontology resolution) and Phase 5 (net-new fields) layer on independently.

---

## 5. Risks & open questions

- **CUI vs CURIE (§0)** — the single biggest branch point. UMLS changes scope, licensing, and effort entirely. **Decide first.**
- **Ontology versioning / drift** — OLS/ontologies change; unpinned online validation is non-reproducible. Mitigation: pin versions in a manifest; prefer offline OAK subsets; record ontology version in the mapping report.
- **Obsolete / merged terms** — some stored CURIEs will be deprecated or replaced over time; V3 surfaces them, but *fixing* them is curation work (needs a human-in-the-loop replacement flow, not silent rewrite).
- **Multi-ontology "EFO ID" column** — condition ids span 15+ prefixes; `id_prefixes` and the prefix map must enumerate all of them (12 are currently undeclared). Treat "condition id" as a prefix-set, not single-ontology.
- **Multi-mapping & positional alignment** — up to 8 body-site / 4 condition ids per experiment; label↔id pairing is positional. UBERON aligns perfectly; **condition labels contain commas** (`Hepatitis, Alcoholic`) so the emitter must reuse `split_comma_strict` and assert equal counts, else fall back rather than mis-pair.
- **Labels without IDs** — ~0.5% of body-site and ~2.3% of condition annotations lack an id; these are genuine curation gaps, not loader bugs. Leave the id slot empty; report them.
- **host_species / location have no ID column at all** — net-new mapping (Phase 5); small and tractable but a distinct, manual effort. `matched_on`/`confounders` mapping is noisy — recommend deferring/skipping.
- **NCBITaxon scale for offline validation** — full NCBITaxon semsql is heavy; use a taxon-slim or a local NCBI names dump for taxon resolution to keep CI light.
- **Taxa CURIE form** — canonical key stays the integer `ncbi_id`; a `NCBITaxon:` view is derived, not a second source of truth (avoid divergence).

### Top 3 decisions to put to the user before Phase 1

1. **CURIE or UMLS CUI?** Recommend CURIE (matches the data, ~99% free). Confirm UMLS is *not* required, or scope it as a separate downstream project.
2. **Schema shape: paired `*_id` slots (recommended, additive, low-risk) vs. a restructuring `OntologyTerm` class (richer per-term provenance, heavier migration)?** Recommend paired slots now, `OntologyTerm` later only if per-term predicate/confidence metadata is needed.
3. **Validation posture: offline OAK + pinned ontology subsets (reproducible, CI-safe — recommended) vs. online OLS4 (always-current, not reproducible)?** And: does ontology-aware validation (V3/V4) gate CI, or is it an advisory report? Recommend offline+pinned, advisory report first, gating later.
