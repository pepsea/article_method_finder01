# SPEC — Method Discovery from Literature

Status: DRAFT v0.1 (2026-11-09). Sections marked **[DECIDE]** need an answer from the owner;
use the stated default until told otherwise and log it in `docs/ASSUMPTIONS.md`.

## 1. Purpose
Given a research topic, find the best method or approach by reading past research.
The system gathers papers, turns them into a searchable and comparable knowledge base,
and produces a cited recommendation of methods per condition.

Primary user: one researcher (the owner), running locally. Not a public service.

## 2. Success criteria (measurable)
| ID | Criterion | Target |
|----|-----------|--------|
| S1 | Recall against a hand-made golden set of known relevant papers (20–30 per test topic) | >= 90% found by the collection stage |
| S2 | Every statement in the final report has a PMID/DOI and a verbatim quote that exists in the stored text | 100% (checked automatically) |
| S3 | Extraction accuracy on a hand-checked random sample (5–10% of papers) | >= 85% of fields correct |
| S4 | Full pipeline re-run from a saved query set reproduces the same paper list | identical IDs |
| S5 | One topic end to end (collect to report) | completes unattended on the owner's PC |

## 3. Scope
In scope: collection, deduplication, screening, full-text parsing, structured extraction,
RAG question answering, method taxonomy, comparison matrix, report, run logging, re-run for updates.

Out of scope (v1): user accounts, cloud deployment, paywalled full text, image/figure mining,
automatic citation-graph visualisation, multi-user editing.

## 4. Sources
| Source | Use | Access | Notes |
|--------|-----|--------|-------|
| PubMed | titles, abstracts, MeSH, PMIDs | NCBI E-utilities (API key) | respect NCBI rate limits |
| PubMed Central (PMC) | full text (JATS XML) | Open Access subset; use the **commercial-use** licence subset for bulk text mining | record licence per paper |
| Europe PMC | full text + preprints, search | REST API | complements PMC |
| arXiv | preprints | official API | about one request per 3 s; preprint flag = true |
| bioRxiv / medRxiv | preprints | official API | preprint flag = true; check licence per paper |
| OpenAlex | citation graph, dedup help | free API | used for snowballing |
| Crossref / Retraction Watch data | retraction flags | Crossref API | mark retracted papers, exclude by default |
| Protocol sources (e.g. Bio-protocol, protocols.io) | method details | **[DECIDE]** add in v2 | check terms first |

Rules: cache every response; store the licence and URL per paper; never bulk-scrape sites
whose terms forbid it; when a source's terms are unclear, skip it and note it in ASSUMPTIONS.

## 5. Pipeline
```
topic -> A keywords -> B collect -> C dedup -> D screen -> E parse -> F extract
      -> G index (RAG) -> H classify -> I synthesise -> J report
                 ^ snowballing from D feeds back to B
```
Each stage reads and writes SQLite tables (section 6) and can be re-run alone.

**A. Keyword generation.** Input: topic text + a few seed terms. A local LLM proposes
synonyms, abbreviations, famous method names, and neighbouring-field terms. Then validate:
add MeSH terms matched from PubMed, keep only terms that return results, store all terms
with their source (`llm`, `mesh`, `seed`, `snowball`). Output: query set saved in `queries`.

**B. Collection.** Run every query on every enabled source. Save raw JSON/XML under `data/raw/`.
Snowballing: for papers passing screening with high relevance, fetch references and citing
papers (OpenAlex) and add them as candidates (max 2 rounds, configurable).

**C. Deduplication.** Match by DOI, then PMID, then normalised title + first author + year.
Link a preprint to its published version; keep the published record as primary and the
preprint as an alias. Flag retractions.

**D. Screening.** Written inclusion/exclusion criteria stored per topic (`topics.criteria`).
The LLM labels each paper `include` / `exclude` / `unsure` with a one-line reason.
`unsure` goes to a review list in the UI. The owner can override any label.

**E. Parsing.** PMC JATS XML to sections (Abstract, Introduction, Methods, Results,
Discussion, tables). PDFs only when XML is missing; mark parse quality. Keep metadata:
year, journal, species/organism, preprint flag, licence.

**F. Structured extraction.** For every included paper, the LLM fills the schema in
section 7 as JSON. Each field has a value and the supporting quote with its location.
Missing information is `null`, never guessed. Validate JSON against the schema; retry once
on failure; otherwise mark `failed`.

**G. Indexing (RAG).** Chunk by section (about 300–500 tokens with overlap, never across
sections). Hybrid retrieval: SQLite FTS5 (keyword) + FAISS (vector), merged and reranked.
Filters: year, source, preprint flag, taxonomy class. Each answer lists the chunks used.

**H. Classification.** The LLM and clustering propose a taxonomy of methods; the owner
reviews and edits it in the UI; then every paper is tagged. The reviewed taxonomy is stored
and reused so labels stay stable across re-runs.

**I. Synthesis.** Build a matrix: rows = method class, columns = condition (from extracted
fields), cells = performance summary + number of papers + evidence grade (section 8) +
list of conflicting papers. Produce a recommendation per condition with a confidence level.

**J. Report.** Markdown and HTML report: topic, search record (queries, dates, counts at
each stage, like a PRISMA flow), taxonomy, matrix, recommendations, conflicts, limitations,
reference list.

## 6. Data model (SQLite)
- `topics(id, name, description, criteria, created_at)`
- `queries(id, topic_id, term, origin, source, run_id, hit_count)`
- `papers(id, doi, pmid, pmcid, arxiv_id, title, authors, year, journal, abstract, source, url, licence, is_preprint, is_retracted, primary_paper_id, status)`
- `screening(paper_id, topic_id, label, reason, reviewer, updated_at)` — reviewer is `llm` or `human`
- `sections(id, paper_id, name, text, parse_quality)`
- `chunks(id, paper_id, section_id, text, token_count)` + FTS5 table + FAISS index keyed by chunk id
- `extractions(id, paper_id, schema_version, json, status, run_id)`
- `taxonomy(id, topic_id, name, parent_id, definition)` and `paper_tags(paper_id, taxonomy_id, confidence, reviewer)`
- `runs(id, stage, started_at, model, model_version, prompt_version, params_json, notes)`

## 7. Extraction schema (v1; every field also has `quote` and `location`)
`method_name`, `method_category`, `objective`, `organism_or_sample`, `key_parameters`
(free-form object), `dataset_or_cohort`, `sample_size`, `comparators`, `performance_metric`
(name, value, unit), `validation_type` (none / internal / independent / experimental),
`reported_limitations`, `software_or_tools`, `code_or_data_availability`.

Rules: copy numbers exactly as written; keep units; do not convert or average at this stage.

## 8. Evidence grade (v1 default, **[DECIDE]** to refine)
- A: >= 3 independent groups, independent or experimental validation, peer-reviewed
- B: 2 groups, or one group with independent validation
- C: single group, internal validation only
- D: preprint only, or no validation
Retracted papers never count.

## 9. Models and tools **[DECIDE]**
Defaults until the owner confirms (verify each licence allows commercial use before use):
- LLM served by Ollama, an open-weight model whose licence allows commercial use
- Embeddings: an open model with MIT or Apache-2.0 licence (e.g. BGE-M3)
- Reranker: an open cross-encoder with a permissive licence
Hardware (GPU/RAM) is unknown: ask the owner and choose model size to fit.
Keep model names in one config file so they can be swapped.

## 10. Interfaces
FastAPI endpoints (JSON) + a plain HTML/JS page:
- create topic / edit criteria, run a stage, view run log
- screening review list, taxonomy editor
- ask a question over the RAG (answer + cited chunks)
- view matrix and download the report

CLI mirrors every stage: `python -m app.cli <stage> --topic <id>`.

## 11. Milestones (build in this order; stop and report after each)
| M | Deliverable | Acceptance test |
|---|-------------|-----------------|
| M0 | Docker skeleton, SQLite schema, config file, health endpoint, test runner | `docker compose up` works; `pytest` passes |
| M1 | Stage A+B for PubMed only (keywords, collect, cache) | golden-set recall printed; re-run gives identical IDs (S4) |
| M2 | Stage C+D (dedup, screening, review list) | duplicates merged on a prepared test list; labels have reasons |
| M3 | Stage E (PMC XML parsing) | sections present for a sample of 20 papers; no text lost in spot check |
| M4 | Stage F (extraction) | S3 on a hand-checked sample; every quote found in stored text (S2 part) |
| M5 | Stage G (RAG) + ask endpoint | answers cite chunks; hybrid beats keyword-only on 10 test questions |
| M6 | Stage H (taxonomy) | owner reviews taxonomy; tags stable on re-run |
| M7 | Stage I+J (matrix, report) | S2 passes automatically; S5 on first topic |
| M8 | Extra sources (Europe PMC, arXiv, bioRxiv/medRxiv, OpenAlex snowballing, retraction flags) | recall rises vs M1 without new duplicates |

## 12. Quality control
- Golden set per topic stored in `tests/golden/<topic>.csv` (PMID or DOI list).
- Automatic check that every quote is a substring of stored text (after whitespace normalisation).
- Random-sample review sheet exported as CSV for the owner (5–10% of extractions).
- Log model name, version, and prompt version for every LLM call.

## 13. Open questions for the owner
1. First test topic and 20–30 known relevant papers for the golden set.
2. Hardware available for local models.
3. Which protocol sources to add after v1.
4. Whether evidence grading in section 8 matches the owner's judgement.
