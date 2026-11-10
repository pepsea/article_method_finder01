"""仕様の残り部分のテスト(通信なし): デザイン(Gemma雛形)・RAG・分類体系・矛盾・参考文献・レビューシート。"""
import csv
import json
import re

import numpy as np

from app import design, knowledge as k, rag, taxonomy
from app.db import init_db


def fake_embed(texts, cfg=None):
    # 語を64次元に散らした簡単なベクトル(本物のモデルの代わり。同じ語を含む文章が近くなる)
    out = np.zeros((len(texts), 64), dtype="float32")
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z0-9]+", t.lower()):
            out[i, hash(w) % 64] += 1
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)


def _topic(conn, pid_texts, with_profile=False):
    # トピック1と、採用済みの論文(id, 要旨)を作る
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'t','c')")
    for i, (title, abstract, year) in pid_texts.items():
        conn.execute("INSERT INTO papers(id, pmid, title, abstract, year, authors) VALUES (?,?,?,?,?,?)", (i, str(100 + i), title, abstract, year, "Doe J; Lee K"))
        conn.execute("INSERT INTO screening(paper_id, topic_id, label, reviewer) VALUES (?,1,'include','llm')", (i,))
        conn.execute("INSERT INTO topic_papers VALUES (1,?)", (i,))


# ---------- デザイン(Gemma雛形) ----------

def test_builtin_template_is_valid_and_complete():
    t = design.builtin_template()
    assert design.validate(t) == []
    assert all("{{" + n + "}}" in t for n in design.REQUIRED)


def test_validate_rejects_unsafe_or_incomplete_templates():
    t = design.builtin_template()
    assert design.validate(t.replace("{{GLANCE}}", "")) != []
    assert any("script" in e for e in design.validate(t.replace("</head>", "<script>x()</script></head>")))
    assert any("external URL" in e for e in design.validate(t.replace("</head>", "<style>a{background:url(https://evil.example/x.png)}</style></head>")))


def test_get_template_retries_then_caches_and_falls_back(tmp_path, monkeypatch):
    # 1回目は不正(script入り)→検査の指摘を伝えて再生成→合格→キャッシュ。次回はモデルを呼ばない。モデル未導入なら既定デザイン
    monkeypatch.setattr(design, "DESIGN_DIR", tmp_path)
    prompts = []
    good = design.builtin_template()
    def fake(cfg, model, prompt):
        prompts.append(prompt)
        return "```html\n" + (good.replace("</head>", "<script>1</script></head>") if len(prompts) == 1 else good) + "\n```"
    monkeypatch.setattr(design, "ask_model", fake)
    html, src = design.get_template({"design_model": "gemma4:12b"})
    assert src == "generated:gemma4:12b" and len(prompts) == 2 and "rejected" in prompts[1]
    html2, src2 = design.get_template({"design_model": "gemma4:12b"})
    assert src2 == "cached:gemma4:12b" and len(prompts) == 2
    def missing(cfg, model, prompt): raise design.ModelMissing(model)
    monkeypatch.setattr(design, "ask_model", missing)
    _, src3 = design.get_template({"design_model": "other:1b"})
    assert src3.startswith("builtin") and "ollama pull other:1b" in src3


def test_render_fills_placeholders_with_verified_content_only():
    parts = {k_: f"<i>{k_}</i>" for k_ in ("findings", "glance", "goal", "limits", "search", "details", "matrix", "conflicts", "taxonomy", "references")}
    out = design.render(design.builtin_template(), parts, "T <x>", "sub", "<a>L</a>")
    assert "{{" not in out and "<i>glance</i>" in out and "T &lt;x&gt;" in out


# ---------- RAG ----------

def test_split_never_exceeds_size_and_overlaps():
    words = [f"w{i}" for i in range(700)]
    chunks = rag.split(" ".join(words))
    assert all(len(c.split()) <= rag.WORDS for c in chunks) and len(chunks) >= 3
    assert chunks[0].split()[-rag.OVERLAP:] == chunks[1].split()[:rag.OVERLAP]


def test_hybrid_search_filters_and_rebuild_with_fulltext():
    conn = init_db(":memory:")
    _topic(conn, {1: ("Urine cotinine", "solid phase extraction of cotinine in urine by LC-MS/MS", 2015),
                  2: ("Hair nicotine", "ultrasonic extraction of nicotine from hair samples", 2020),
                  3: ("Plasma assay", "protein precipitation with acetonitrile of plasma", 2021)})
    rag.build_index(conn, 1, embed_fn=fake_embed)
    r = rag.search(conn, 1, "hair ultrasonic extraction", k=2, embed_fn=fake_embed)
    assert r[0]["pmid"] == "102" and r[0]["via"] in ("both", "keyword")
    assert all(c["year"] >= 2020 for c in rag.search(conn, 1, "extraction", year_min=2020, embed_fn=fake_embed))
    assert rag.search(conn, 1, "extraction", mode="vector", embed_fn=fake_embed)
    # 全文が後から入ったら、その論文だけ節つきで作り直される
    conn.execute("INSERT INTO sections(paper_id, name, text) VALUES (2,'Methods','hair was washed then sonicated in methanol for 18 h before analysis')")
    out = rag.build_index(conn, 1, embed_fn=fake_embed)
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE paper_id=2 AND section_id IS NOT NULL").fetchone()[0] == 1 and out["embedded"] >= 1
    assert rag.search(conn, 1, "sonicated methanol", k=1, embed_fn=fake_embed)[0]["section"] == "Methods"
    assert rag.evaluate(conn, 1, [("hair ultrasonic", 102)], embed_fn=fake_embed)["hybrid"] == "1/1"


def test_ask_marks_invented_quotes_as_unverified(monkeypatch):
    conn = init_db(":memory:")
    _topic(conn, {1: ("Hair nicotine", "ultrasonic extraction of nicotine from hair samples", 2020)})
    rag.build_index(conn, 1, embed_fn=fake_embed)
    monkeypatch.setattr(rag, "search", lambda *a, **kw: [{"chunk_id": 1, "pmid": "101", "year": 2020, "section": "Abstract", "title": "t",
                                                          "text": "ultrasonic extraction of nicotine from hair samples", "score": 1, "via": "both"}])
    class R:
        def raise_for_status(self): pass
        def json(self): return {"response": json.dumps({"answer": "Ultrasonic [1].", "evidence": [{"n": 1, "quote": "ultrasonic extraction of nicotine"}, {"n": 1, "quote": "made up sentence"}]})}
    monkeypatch.setattr(rag.httpx, "post", lambda *a, **kw: R())
    res = rag.ask(conn, 1, "how was hair extracted?")
    assert [e["verified"] for e in res["evidence"]] == [True, False]


# ---------- 分類体系 / 矛盾 / 参考文献 / レビューシート ----------

def _extract(conn, pid, prep, lloq=None, cond="urine", author="Smith A"):
    # 抽出済みの知識を1件作る(検証済みの項目つき)
    def f(v): return {"value": v, "quote": v, "location": "Abstract", "verified": v is not None}
    fields = {"matrix": f(cond), "analytes": f("nicotine, cotinine"), "sample_preparation": f(prep), "chromatography_column": f("C18"),
              "internal_standard": f("cotinine-d3"), "lloq": f(lloq), "validation_type": {"value": "internal", "quote": None, "location": None, "verified": False, "inferred": True}}
    conn.execute("INSERT INTO extractions(paper_id, schema_version, json, status) VALUES (?,?,?, 'ok')", (pid, "1.1", json.dumps({"fields": fields, "rejected": 0})))
    conn.execute("UPDATE papers SET authors=? WHERE id=?", (f"X Y; {author}", pid))


def test_taxonomy_propose_apply_is_stable():
    conn = init_db(":memory:")
    _topic(conn, {i: (f"p{i}", "abs", 2020) for i in range(1, 11)})
    for i in range(1, 11):
        _extract(conn, i, "enzymatic hydrolysis with subtilisin" if i <= 6 else "freeze-drying and milling")
    props = taxonomy.propose(conn, 1, embed_fn=fake_embed, ask_fn=lambda prompt: {"name": "enzymatic hydrolysis", "regex": "enzym|subtilisin|hydrolys", "definition": "d"})
    assert props and props[0]["regex_coverage"] > 0
    res = taxonomy.apply(conn, 1, [{**props[0], "accept": True}])
    assert "enzymatic hydrolysis" in res["added"] and res["tagged"] == 10
    again = taxonomy.tag_papers(conn, 1)          # 再実行しても同じタグ
    assert again == {"classes": res["classes"], "tagged": 10}


def test_conflicts_references_and_review_sheet(tmp_path):
    conn = init_db(":memory:")
    _topic(conn, {i: (f"Title {i}", "abs", 2020 + i) for i in range(1, 4)})
    _extract(conn, 1, "solid-phase extraction", "0.1 ng/mL", author="A One")
    _extract(conn, 2, "solid-phase extraction", "5 ng/mL", author="B Two")      # 50倍ずれる → 矛盾
    _extract(conn, 3, "solid-phase extraction", "0.2 ng/mL", author="C Three")
    syn = k.synthesize(conn, 1)
    cf = syn["recs"]["urine"]["conflicts"]
    assert len(cf) == 1 and cf[0]["ratio"] == 50
    refs = k.references(conn, ["101", "102", "999"])
    assert [r["pmid"] for r in refs] == ["101", "102"] and refs[0]["authors"].endswith("et al.")
    out = k.review_sheet(conn, 1, tmp_path, frac=1.0)
    assert out["papers"] == 3
    rows = list(csv.DictReader(open(out["path"])))
    for r in rows[:4]: r["correct (y/n)"] = "y"
    rows[4]["correct (y/n)"] = "n"
    with open(out["path"], "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    sc = k.score_review_sheet(out["path"])
    assert sc["checked"] == 5 and sc["accuracy"] == 0.8 and not sc["target_met"]
