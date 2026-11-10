# CLAUDE.md — Method Discovery from Literature

Full blueprint: @docs/SPEC.md (read it before any work; it is the source of truth).

## What this project is
A local pipeline that, for a given research topic, collects past papers, builds a searchable
text base (RAG), extracts methods into a structured table, classifies them, and reports
the best method per condition with evidence and citations.

## Stack (do not change without asking)
- Python 3.11+, FastAPI, SQLite (FTS5 for keyword search), FAISS for vectors
- Plain HTML/JS front end (no framework), Docker (`docker compose up`)
- Local LLM and embeddings served locally (default: Ollama). No cloud LLM calls for paper text.
- Commercial-use only: every dependency, model, and dataset must allow commercial use.
  Record the licence in `docs/LICENSES.md` when adding one. If unsure, say so and ask.

## Working rules
1. Build the simplest version that works first. Minimal code, no speculative features (YAGNI).
2. Work milestone by milestone in the order of SPEC section 11. Stop after each milestone,
   run its acceptance test, and report the result before starting the next.
3. Write a plain-language comment above every code block explaining what it does and why.
   In notebooks, add a short markdown explanation above every cell.
4. Every claim stored or shown to the user must carry its source (PMID/DOI + quote).
   Never fabricate a citation, a number, or a quote. If the source lacks it, store `null`.
5. Respect each source's rate limits and terms (see SPEC section 4). Cache all downloads.
6. Log every run (queries, date, model name/version, prompt version) to the `runs` table.
7. Do not delete or overwrite raw downloaded data; derived data must be rebuildable from it.
8. When a requirement is unclear or conflicts with SPEC, ask instead of guessing.
   When you assume something, write it in `docs/ASSUMPTIONS.md`.

## Commands
- Start everything: `docker compose up --build`
- Run tests: `pytest -q`
- Rebuild derived data from raw: `python -m app.cli rebuild`

## Layout
```
app/            FastAPI app and pipeline code (one module per stage)
data/raw/       downloaded papers, never edited
data/db/        SQLite file and FAISS index (rebuildable)
docs/           SPEC.md, ASSUMPTIONS.md, LICENSES.md
tests/          unit tests and the golden-set recall test
web/            plain HTML/JS
```
