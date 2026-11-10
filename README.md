# Method Discovery from Literature (local)

Needs: Ollama running locally with the models in `config.yaml` (qwen3:8b, qwen3:14b, bge-m3). Internet for PubMed.

## One-shot (recommended)
Works for any topic: the system drafts a topic profile (what to extract, how to classify, how to expand searches) and screening criteria with the LLM. Edit `profiles/<id>.json` afterwards if you like (`./run_local.sh set-profile --topic <id> --file profiles/<id>.json`).
```
./run_local.sh create-topic --name "My topic" --seeds "term A AND term B;term C"   # also drafts profile + criteria (a few minutes)
./run_local.sh list-topics; ./run_local.sh show-profile --topic 2; ./run_local.sh show-criteria --topic 2
./run_local.sh keywords --topic 1                    # A: LLM + MeSH keyword generation
./run_local.sh set-criteria --topic 1 --file my_criteria.md   # optional: your own criteria instead of the drafted ones
./run_local.sh loop --topic 1 --rounds 2             # collect -> dedup -> screen -> full text -> extract -> report -> new queries -> repeat
```
The loop takes hours (about 4 s per screened paper, 30 s per extracted paper). It is resumable: run it again and it continues.

## Single stages
```
./run_local.sh collect --topic 1       # B: PubMed collection (cached in data/raw)
./run_local.sh dedup                   # C: duplicates
./run_local.sh screen --topic 1 [--limit 500]   # D: include/exclude/unsure
./run_local.sh fulltext --topic 1      # full text: PubMed Central, then Europe PMC (CC BY-type licences only)
./run_local.sh extract --topic 1 [--limit 200]  # structured extraction with verified quotes
./run_local.sh report --topic 1        # synthesis + goal check + report.html in a new dated folder
./run_local.sh export --topic 1        # dated snapshot of papers/queries only
./run_local.sh serve                   # history page: http://localhost:8000/
```

## Where results are
`searches/<id>_<name>/<date-time>/` : `report.html` (answer), `included.csv`, `papers.csv`, `review_unsure.csv`, `queries.csv`, `criteria.txt`, `summary.json`, `goal_check.json`.
Open http://localhost:8000/ for the history page (view papers, override labels, save snapshots, open reports).

Tests: `.venv/bin/python -m pytest -q`. Decisions and assumptions: `docs/ASSUMPTIONS.md`.

## Retrieval, taxonomy, summary
```
./run_local.sh index --topic 1                          # G: chunk abstracts + full text, FTS5 + FAISS index (bge-m3)
./run_local.sh ask --topic 1 --question "How is hair prepared?"   # answer with verified citations
./run_local.sh taxonomy --topic 1                       # H: tag papers with the taxonomy
./run_local.sh summary --topic 1                        # one-page summary.html (design by gemma4:12b, content verified)
```
The summary's design (HTML/CSS template) is written by `gemma4:12b` (`ollama pull gemma4:12b`, ~8 GB); the content is filled in by code from verified data, so the model cannot invent numbers or citations. Without the model, a built-in design is used.

## Start from a question
```
./run_local.sh from-question --question "What is the best sample preparation for cotinine in saliva by LC-MS/MS?"
./run_local.sh ask --topic 1 --question "How is hair prepared?" --expand     # collects more papers if the knowledge base has no verified evidence
```
The question becomes PubMed queries (checked by hit counts), then a topic. In the notebook set `RESEARCH_QUESTION` (section 2b) and `EXPAND_QUESTIONS`.

## Jupyter notebook
`method_finder.ipynb` runs every stage of the specification, step by step: settings, keywords, collect, screen and review, full-text fetch and full-text reading, extraction, RAG index and cited Q&A, taxonomy, synthesis with conflicts and goal check, heatmap, quality control (review sheet, golden-set recall), full report and Gemma-designed summary, optional loop, history.
```
./run_local.sh notebook      # opens Jupyter in the project folder
```
Edit the settings cell at the top; run cells from top to bottom. Every stage is resumable.
