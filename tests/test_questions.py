"""質問から検索を作る機能のテスト(LLM・PubMedは差し替え)。"""
import json

from app import questions as qs
from app.db import init_db


class FakeResp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return {"response": json.dumps(self.payload)}


def test_queries_are_validated_by_hit_counts(monkeypatch):
    # 件数が少なすぎる/多すぎる式は seeds に入らない
    monkeypatch.setattr(qs.httpx, "post", lambda *a, **k: FakeResp({"topic_name": "Cotinine in DBS", "queries": ["a AND b", "c", "d AND e", "a AND b"]}))
    counts = {"a AND b": 120, "c": 90000, "d AND e": 2}
    monkeypatch.setattr(qs.pubmed, "search", lambda q, end, **kw: {"count": counts[q]})
    r = qs.queries_from_question("How to measure cotinine in dried blood spots?")
    assert r["topic_name"] == "Cotinine in DBS" and r["seeds"] == ["a AND b"]
    assert [x["status"] for x in r["queries"]] == ["ok", "too broad", "too narrow"]   # 重複は1つにまとめる


def test_topic_from_question_requires_a_usable_query(monkeypatch):
    conn = init_db(":memory:")
    monkeypatch.setattr(qs, "queries_from_question", lambda q: {"topic_name": "T", "queries": [{"query": "x", "count": 0, "status": "too narrow"}], "seeds": []})
    try:
        qs.topic_from_question(conn, "q")
        assert False, "should raise"
    except ValueError as e:
        assert "too narrow" in str(e)


def test_ask_expands_only_when_evidence_is_missing(monkeypatch):
    # 検証済みの根拠があれば追加収集しない。無ければ追加収集して答え直す
    calls = {"expand": 0}
    good = {"answer": "a", "evidence": [{"n": 1, "pmid": "1", "quote": "q", "verified": True}], "chunks": []}
    bad = {"answer": "unknown", "evidence": [{"n": 1, "pmid": "1", "quote": "q", "verified": False}], "chunks": []}
    answers = iter([bad, good])
    monkeypatch.setattr(qs.rag, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(qs, "expand_for_question", lambda *a, **k: calls.__setitem__("expand", calls["expand"] + 1) or {"new_papers": 3})
    out = qs.ask_with_expansion(None, 1, "q")
    assert calls["expand"] == 1 and out["expanded"]["new_papers"] == 3 and out["final"] is good
    monkeypatch.setattr(qs.rag, "ask", lambda *a, **k: good)
    assert qs.ask_with_expansion(None, 1, "q")["expanded"] is None and calls["expand"] == 1
    assert qs.ask_with_expansion(None, 1, "q", expand=False)["expanded"] is None


def test_expand_adds_only_ok_queries_and_runs_the_pipeline(monkeypatch):
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name) VALUES (1,'Topic')")
    monkeypatch.setattr(qs, "queries_from_question", lambda q, context=None, anchor_terms=None: {"topic_name": "x", "seeds": ["good"], "queries": [
        {"query": "good", "count": 50, "status": "ok"}, {"query": "huge", "count": 99999, "status": "too broad"}]})
    from app import querydesign
    monkeypatch.setattr(querydesign, "gate", lambda c, t, terms, origin="snowball", judge_fn=None: list(terms))
    seen = []
    monkeypatch.setattr(qs.stages, "collect", lambda c, t: seen.append("collect") or {"added": 7})
    monkeypatch.setattr(qs.stages, "dedup", lambda c: seen.append("dedup"))
    monkeypatch.setattr(qs.stages, "screen", lambda c, t, limit=None, boost=None: seen.append(f"screen{limit}") or {})
    monkeypatch.setattr(qs.fulltext, "fetch_all", lambda c, t: seen.append("fulltext") or {})
    monkeypatch.setattr(qs.knowledge, "extract_all", lambda c, t, lim: seen.append(f"extract{lim}") or {})
    monkeypatch.setattr(qs.rag, "build_index", lambda c, t: seen.append("index") or {})
    out = qs.expand_for_question(conn, 1, "q", screen_limit=10, extract_limit=3)
    assert out["new_queries"] == ["good"] and out["new_papers"] == 7
    assert seen == ["collect", "dedup", "screen10", "fulltext", "extract3", "index"]


def test_anchor_keeps_queries_on_the_topic(monkeypatch):
    # トピックの対象語を AND で付ける。語幹にならない正規表現(記号入り)は使わない
    from app import profile as prof
    assert qs.anchor(prof.NICOTINE) == "(nicotin* OR cotinin* OR anabasin* OR anatabin*)"
    assert qs.anchor({"entity_regex": "a.+b|[x]"}) is None
    sent = []
    monkeypatch.setattr(qs.httpx, "post", lambda *a, **k: FakeResp({"topic_name": "T", "queries": ["dried blood spot AND LC-MS/MS"]}))
    monkeypatch.setattr(qs.pubmed, "search", lambda q, end, **kw: sent.append(q) or {"count": 50})
    r = qs.queries_from_question("q", anchor_terms="(nicotin*)")
    assert r["seeds"] == ["(dried blood spot AND LC-MS/MS) AND (nicotin*)"]


def test_question_keywords_skip_stop_words():
    assert qs.keywords_of("What is the best sample preparation for cotinine in dried blood spots by LC-MS/MS?") == ["sample", "preparation", "cotinine", "dried", "blood", "spots", "lc-ms"]


def test_screen_limit_counts_only_llm_calls_and_boost_orders(monkeypatch):
    # ルール除外は上限に数えない。質問の語を多く含む論文を先にLLMで判定する
    from app import stages
    from app.db import init_db
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'t','c')")
    rows = [(1, "Off topic A", "no relevant words"), (2, "Off topic B", "nothing"), (3, "Nicotine plain", "nicotine in urine"),
            (4, "Nicotine dried blood spot", "nicotine measured in dried blood spots")]
    for i, t, a in rows:
        conn.execute("INSERT INTO papers(id, pmid, title, abstract) VALUES (?,?,?,?)", (i, str(i), t, a))
        conn.execute("INSERT INTO topic_papers VALUES (1,?)", (i,))
    order = []
    monkeypatch.setattr(stages.httpx, "get", lambda *a, **k: type("R", (), {"json": lambda s: {"models": []}})())
    monkeypatch.setattr(stages, "_screen_one", lambda cfg, crit, p: order.append(p["id"]) or ("include", "r"))
    stages.screen(conn, 1, limit=1, boost=["dried", "blood", "spots"])
    assert order == [4]                                   # 質問の語が多い論文が先
    labels = dict(conn.execute("SELECT paper_id, reviewer FROM screening").fetchall())
    assert labels == {1: "rule", 2: "rule", 4: "llm"}     # 上限1でも、ルール除外の2件は処理される
