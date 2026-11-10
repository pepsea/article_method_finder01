"""M2 テスト(通信なし): 重複排除・ルール除外・履歴の保存と上書きAPI。"""
from fastapi.testclient import TestClient

from app import export, stages
from app.db import init_db


def _add(conn, **kw):
    # テスト用に論文を1件入れる
    cols = ",".join(kw)
    conn.execute(f"INSERT INTO papers({cols}) VALUES ({','.join('?' * len(kw))})", tuple(kw.values()))


def test_dedup_by_doi_pmid_and_title():
    # 準備した一覧: DOI一致 / PMID一致 / 題名+筆頭著者+年一致 / 無関係、の4パターン
    conn = init_db(":memory:")
    _add(conn, pmid="1", doi="10.1/a", title="Nicotine in urine", authors="Smith J", year=2020)
    _add(conn, pmid="2", doi="10.1/a", title="Other title", authors="X Y", year=2021)      # DOI一致
    _add(conn, pmid="1", doi=None, title="Zzz", authors="Q R", year=2019)                   # PMID一致
    _add(conn, pmid="9", doi=None, title="NICOTINE in Urine!", authors="Smith J", year=2020)  # 題名一致
    _add(conn, pmid="4", doi="10.1/d", title="Different paper", authors="Lee K", year=2018)
    r = stages.dedup(conn)
    assert r["merged"] == 3
    primaries = conn.execute("SELECT COUNT(*) FROM papers WHERE primary_paper_id IS NULL").fetchone()[0]
    assert primaries == 2


def test_rule_excludes_papers_without_analyte(monkeypatch):
    # 解析対象の語が無い論文はLLMを呼ばずに除外される(LLMが呼ばれたらテスト失敗)
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'t','c')")
    _add(conn, pmid="1", title="Protein folding", abstract="No relevant compound here.")
    conn.execute("INSERT INTO topic_papers VALUES (1, 1)")  # このトピックが集めた論文
    monkeypatch.setattr(stages.httpx, "get", lambda *a, **k: type("R", (), {"json": lambda s: {"models": []}})())
    monkeypatch.setattr(stages, "_screen_one", lambda *a: (_ for _ in ()).throw(AssertionError("LLM called")))
    stages.screen(conn, 1)
    row = conn.execute("SELECT label, reviewer FROM screening").fetchone()
    assert (row["label"], row["reviewer"]) == ("exclude", "rule")


def test_export_and_override_api(tmp_path, monkeypatch):
    # 保存すると日時フォルダにCSVができ、履歴APIに出る。人の上書きは reviewer=human になる
    import app.db
    import app.main
    monkeypatch.setattr(app.db, "DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(export, "SEARCH_DIR", tmp_path / "searches")
    conn = init_db()
    conn.execute("INSERT INTO topics(id, name) VALUES (1,'My Topic')")
    _add(conn, pmid="5", title="A", year=2020)
    conn.execute("INSERT INTO screening VALUES (1,1,'unsure','short abstract','llm',CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()
    c = TestClient(app.main.app)
    folder = c.post("/api/searches/1/export").json()["folder"]
    assert (tmp_path / "searches").exists() and "my-topic" in folder
    assert c.get("/api/searches").json()[0]["snapshots"]
    assert c.post("/api/screening/override", json={"paper_id": 1, "topic_id": 1, "label": "include"}).json()["ok"]
    p = c.get("/api/searches/1/papers?label=include").json()["papers"][0]
    assert p["reviewer"] == "human"
    assert c.post("/api/screening/override", json={"paper_id": 1, "topic_id": 1, "label": "bad"}).status_code == 400
