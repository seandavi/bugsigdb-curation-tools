# BugSigDB curation — workflow & CLI map

Two views of the system. Rendered by GitHub (and any Mermaid-aware viewer); the
`elk` renderer is requested for a cleaner layout, falling back to the default
engine where `elk` isn't bundled. Kept in sync with the plans in
[`docs/plans/`](plans/) and the ledger [`docs/LEDGER.md`](LEDGER.md).

---

## 1. `bugsigdb` commands & data flow

Rounded = a CLI command; `[/…/]` = a data artifact; `[( … )]` = an external
source or store. The **held-out gold** (relational CSVs + PMID→PMCID map) flows
**only** to `eval score`, never to `curate` — the §6e data firewall.

```mermaid
%%{init: {'flowchart': {'defaultRenderer': 'elk'}}}%%
flowchart LR
  subgraph ingest["Data ingest"]
    SRC[("waldronlab/bugsigdbexports")]
    EXPORT("bugsigdb export")
    DUMP[/"full_dump.csv"/]
    SPLIT("bugsigdb split")
    LOAD("bugsigdb load")
    VALIDATE("bugsigdb validate")
    PMCMAP("bugsigdb pmc-map")
    NESTED[/"nested Study→Exp→Sig (YAML/JSON)"/]
    SRC --> EXPORT --> DUMP
    DUMP --> SPLIT
    DUMP --> LOAD --> NESTED --> VALIDATE
  end

  subgraph gold["HELD-OUT GOLD — scoring only (§6e firewall)"]
    REL[/"relational CSVs: studies · experiments · signatures · signatures_taxa · taxa"/]
    MAP[/"pmid_pmcid_map.csv"/]
  end
  SPLIT --> REL
  REL --> PMCMAP --> MAP

  subgraph taxo["Taxonomy backend (local, offline)"]
    NCBI[("NCBI taxdump (pinned release)")]
    TBUILD("bugsigdb taxonomy build")
    TDB[("ncbi-taxdump-*.duckdb")]
    TLOOKUP("bugsigdb taxonomy lookup")
    NCBI --> TBUILD --> TDB --> TLOOKUP
  end

  subgraph denovo["De-novo curation & evaluation"]
    PMID(["PMID"])
    CURATE("bugsigdb curate — Design-1, Gemini via LiteLLM")
    PRED[/"prediction record (nested-dict)"/]
    SCORE("bugsigdb eval score")
    REPORT[/"per-study JSONL · Markdown · HTML report"/]
    PMID --> CURATE --> PRED --> SCORE --> REPORT
  end

  TDB -. "name→taxid (gap-fill: live NCBI)" .-> CURATE
  TDB -. "name→taxid" .-> SCORE
  REL --> SCORE
  MAP --> SCORE
```

---

## 2. Design-1 (Fused-Lean) curation stage DAG

The per-PMID pipeline. Solid arrows = data flow S0→S9; dashed = a shared
service. The curator receives **only** a PMID + the source it fetches itself.

```mermaid
%%{init: {'flowchart': {'defaultRenderer': 'elk'}}}%%
flowchart TD
  PMID(["PMID"]) --> S0["S0 · resolve<br/>NCBI idconv → PMCID"]
  S0 --> S1["S1 · evidence assembly<br/>EuropePMC fullTextXML +<br/>PMC figures (REST)"]
  S1 --> S2["S2 · study metadata"]
  S2 --> S3["S3 · segment experiments"]
  S3 --> S4["S4 · experiment metadata"]
  S4 --> S5a["S5a · locate DA artifact"]
  S5a --> S5b["S5b · fused extract + verify<br/>taxa · direction · NCBI id"]
  S5b --> S8["S8 · assemble<br/>nested-dict record"]
  S8 --> S9["S9 · validate<br/>(bugsigdb validate)"]
  S9 --> PRED[/"prediction record"/]

  LLM[("Gemini via LiteLLM")] -. "extraction / classification" .-> S2
  LLM -.-> S3
  LLM -.-> S4
  LLM -.-> S5b
  TDB[("TaxonomyDB<br/>local; live NCBI gap-fill")] -. "verify / resolve taxid" .-> S5b

  PRED --> EVAL["bugsigdb eval score<br/>vs held-out gold"]
```

---

*Not shown (deferred): supplement fetching (S1b), the Split-Verify / Split-Panel
designs' verifier & reviewer stages (S10), and ontology CURIE mapping (S7). See
`docs/plans/de-novo-curation-workflow-plan.md` §6.*
