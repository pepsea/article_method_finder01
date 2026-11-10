"""検索語の設計・決定ワークフローのテスト(LLM・PubMedは差し替え)。トピックの内容がコードに書かれていないことも確認する。"""
import json

from app import querydesign as qd, stages
from app.db import init_db

DEFN = {"question": "Which method best detects X in Y?", "topic_name": "X in Y", "goal": "g", "subject_terms": ["alpha", "alpha beta"],
        "method_terms": ["assay", "lc-ms"], "condition_terms": ["serum", "urine sample"], "exclude_terms": ["veterinary"]}


def test_orx_quotes_phrases():
    assert qd.orx(["a", "b c", "d/e"]) == '(a OR "b c" OR "d/e")'


def test_candidates_are_built_from_the_definition_only():
    c = qd.candidates(DEFN, with_mesh=False, with_llm=False)
    q = [x["query"] for x in c]
    assert q[0] == '(alpha OR "alpha beta") AND (assay OR lc-ms)'                              # core(除外語は検索式に入れない)
    assert not any("NOT" in x for x in q)
    assert any(x.startswith("(alpha) AND (assay)") for x in q)                                 # 最も狭い組
    many = qd.candidates({**DEFN, "method_terms": [f"m{i}" for i in range(8)], "condition_terms": []}, with_mesh=False, with_llm=False)
    assert [x["why"] for x in many].count("method: m7") == 1                                   # 8番目の手法語(具体的な手法名)も落とさない
    assert any('AND ("urine sample")' in x for x in q) and any("AND (serum)" in x for x in q)  # 条件ごと
    assert any(x == '(alpha OR "alpha beta") AND (assay)' for x in q) and any(x == '(alpha OR "alpha beta") AND (lc-ms)' for x in q)  # 手法ごと
    withx = qd.candidates(DEFN, with_mesh=False, with_llm=False, exclusions_in_queries=True)[0]["query"]
    assert withx.endswith("AND NOT (veterinary)")                                                # 明示したときだけ
    assert all(x["origin"] == "seed" and x["why"] for x in c)
    assert "nicotin" not in json.dumps(c).lower()                                              # 特定トピックの語が混ざらない


def test_candidates_without_method_terms():
    c = qd.candidates({**DEFN, "method_terms": [], "condition_terms": [], "exclude_terms": []}, with_mesh=False, with_llm=False)
    assert [x["query"] for x in c] == ['(alpha OR "alpha beta")', "(alpha)"]


def test_define_validates_and_normalises(monkeypatch):
    class R:
        def __init__(s, p): s.p = p
        def raise_for_status(s): pass
        def json(s): return {"response": json.dumps(s.p)}
    monkeypatch.setattr(qd.httpx, "post", lambda *a, **k: R({"topic_name": " T ", "subject_terms": ["a", "a", " ", "b"], "method_terms": "oops", "condition_terms": ["c"]}))
    d = qd.define("q?")
    assert d["topic_name"] == "T" and d["subject_terms"] == ["a", "b"] and d["method_terms"] == [] and d["exclude_terms"] == []
    monkeypatch.setattr(qd.httpx, "post", lambda *a, **k: R({"subject_terms": []}))
    try:
        qd.define("q?"); assert False
    except ValueError:
        pass


def _fake_pubmed(monkeypatch, universe):
    # universe: {式: PMIDのリスト}
    def search(q, end, retmax=0, retstart=0):
        ids = universe[q]
        return {"count": len(ids), "ids": ids[:retmax] if retmax else [], "translation": ""}
    monkeypatch.setattr(qd.pubmed, "search", search)
    monkeypatch.setattr(qd.pubmed, "fetch", lambda pmids: [{"pmid": p, "title": f"t{p}", "abstract": "a", "year": 2020} for p in pmids])
    monkeypatch.setattr(qd, "settings", lambda: {"pilot_n": 10, "min_precision": 0.3, "min_novelty": 0.2, "max_total_papers": 4000, "min_hits": 5, "max_candidates": 12})


def test_evaluate_shortcuts_and_precision(monkeypatch):
    _fake_pubmed(monkeypatch, {"few": ["1", "2"], "good": [str(i) for i in range(1, 51)], "huge": [str(i) for i in range(5000)]})
    judge = lambda p: "include" if int(p["pmid"]) % 2 == 0 else "exclude"   # 偶数が関連
    assert qd.evaluate({"query": "few", "origin": "seed", "why": ""}, judge)["reason"].startswith("too narrow")
    assert qd.evaluate({"query": "huge", "origin": "seed", "why": ""}, judge)["reason"].startswith("too broad")
    r = qd.evaluate({"query": "good", "origin": "seed", "why": ""}, judge)
    assert r["sampled"] == 10 and 0 <= r["precision"] <= 1 and r["count"] == 50
    assert qd.evaluate({"query": "good", "origin": "seed", "why": ""}, judge)["precision"] == r["precision"]   # 固定seedで再現


def test_decide_greedy_with_reasons():
    mk = lambda q, ids, prec, status=None: {"query": q, "ids": ids, "count": len(ids), "precision": prec, "sampled": 10, "included": round(prec * 10), "status": status, "reason": None}
    A = mk("A", [str(i) for i in range(100)], 0.9)
    B = mk("B", [str(i) for i in range(5, 100)], 0.8)          # Aとほぼ同じ → 重複
    C = mk("C", [str(i) for i in range(500, 600)], 0.1)        # 関連率が低い
    D = mk("D", [str(i) for i in range(1000, 1100)], 0.6)      # 新規で関連 → 採用
    E = mk("E", [], 0.0, "dropped"); E["reason"] = "too narrow: 0 hits"
    rows = qd.decide([A, B, C, D, E], st={"min_precision": 0.3, "min_novelty": 0.2, "max_total_papers": 4000})
    st = {r["query"]: (r["status"], r["reason"]) for r in rows}
    assert st["A"][0] == "kept" and st["D"][0] == "kept"
    assert st["B"][0] == "dropped" and st["B"][1].startswith("redundant")
    assert st["C"][0] == "dropped" and st["C"][1].startswith("low relevance")
    assert st["E"] == ("dropped", "too narrow: 0 hits")
    # 合計の上限
    capped = qd.decide([mk("A", [str(i) for i in range(100)], 0.9), mk("D", [str(i) for i in range(1000, 1100)], 0.6)], st={"min_precision": 0.3, "min_novelty": 0.2, "max_total_papers": 150})
    assert [r["status"] for r in capped] == ["kept", "dropped"] and capped[1]["reason"].startswith("budget")


def test_design_register_collect_and_override(monkeypatch):
    # 設計 → 保存 → 収集は「採用」だけ → 人の上書きで変わる
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria, description) VALUES (1,'T','c',?)", (json.dumps(DEFN),))
    ids = {"keep me": [str(i) for i in range(1, 21)], "drop me": [str(i) for i in range(100, 120)]}
    _fake_pubmed(monkeypatch, ids)
    judge = lambda p: "include" if int(p["pmid"]) < 100 else "exclude"
    cands = [{"query": "keep me", "origin": "seed", "why": "w"}, {"query": "drop me", "origin": "llm", "why": "w"}]
    table = qd.design(conn, 1, cands, judge_fn=judge)
    st = {r["query"]: r["status"] for r in table}
    assert st == {"keep me": "kept", "drop me": "dropped"}
    assert conn.execute("SELECT reason FROM queries WHERE term='drop me'").fetchone()[0].startswith("low relevance")
    seen = []
    monkeypatch.setattr(stages.pubmed, "search", qd.pubmed.search)
    monkeypatch.setattr(stages.pubmed, "fetch", lambda pm: seen.append(list(pm)) or [])
    stages.collect(conn, 1)
    assert sorted(sum(seen, [])) == sorted(ids["keep me"])               # 不採用の式の論文は集めない
    assert qd.override(conn, 1, keep=["drop me"], drop=["keep me"]) == 2
    assert {r["query"]: r["status"] for r in qd.table(conn, 1)} == {"keep me": "dropped", "drop me": "kept"}


def test_gate_skips_existing_and_returns_only_kept(monkeypatch):
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'T','c')")
    conn.execute("INSERT INTO queries(topic_id, term, origin, hit_count) VALUES (1,'old','seed',3)")
    _fake_pubmed(monkeypatch, {"new good": [str(i) for i in range(1, 21)], "new bad": [str(i) for i in range(100, 120)], "old": ["1", "2", "3"]})
    judge = lambda p: "include" if int(p["pmid"]) < 100 else "exclude"
    kept = qd.gate(conn, 1, ["old", "new good", "new bad"], judge_fn=judge)
    assert kept == ["new good"]
    assert conn.execute("SELECT COUNT(*) FROM queries WHERE term='old'").fetchone()[0] == 1   # 既存は重複して入れない


def test_design_returns_rows_when_some_candidates_are_dropped_early(monkeypatch):
    # 回帰: 件数で早く落とした式(too narrow/broad)には novelty が無く、以前は KeyError で落ちた
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'T','c')")
    _fake_pubmed(monkeypatch, {"tiny": ["1"], "good": [str(i) for i in range(1, 21)]})
    table = qd.design(conn, 1, [{"query": "tiny", "origin": "seed", "why": ""}, {"query": "good", "origin": "seed", "why": ""}], judge_fn=lambda p: "include")
    assert {r["query"]: r["status"] for r in table} == {"tiny": "dropped", "good": "kept"}
    assert next(r for r in table if r["query"] == "tiny")["novelty"] is None


def test_design_retries_with_relaxed_queries_when_nothing_is_kept(monkeypatch):
    # 全候補が落ちたら、緩めた式で一度だけ再挑戦する
    conn = init_db(":memory:")
    d = {**DEFN, "exclude_terms": []}
    conn.execute("INSERT INTO topics(id, name, criteria, description) VALUES (1,'T','c',?)", (json.dumps(d),))
    universe = {"tiny": ["1"]}                                         # 最初の候補は too narrow
    for c in qd.relaxed_candidates(d):
        universe[c["query"]] = [str(i) for i in range(1, 21)]          # 緩めた式は十分な件数
    _fake_pubmed(monkeypatch, universe)
    monkeypatch.setattr(qd, "candidates", lambda defn, **kw: [{"query": "tiny", "origin": "seed", "why": "w"}])
    table = qd.design(conn, 1, judge_fn=lambda p: "include")
    assert [r["status"] for r in table][0] == "dropped"
    assert any(r["status"] == "kept" and r["why"].startswith("relaxed") for r in table)
    assert conn.execute("SELECT COUNT(*) FROM queries WHERE topic_id=1 AND status='kept'").fetchone()[0] >= 1


def test_exclude_terms_go_into_the_criteria_not_the_queries(monkeypatch):
    conn = init_db(":memory:")
    monkeypatch.setattr(qd.stages, "setup_topic", lambda c, n, s, crit=None, pf=None: c.execute("UPDATE topics SET criteria='INCLUDE x' WHERE name=?", (n,)))
    tid = qd.new_topic(conn, DEFN)
    crit = conn.execute("SELECT criteria FROM topics WHERE id=?", (tid,)).fetchone()[0]
    assert crit.endswith("EXCLUDE also papers that are mainly about: veterinary.")
