"""M0 受け入れテスト: スキーマ・設定・health。"""
from fastapi.testclient import TestClient

from app.config import load_config
from app.db import init_db
from app.main import app

# SPEC 第6章の全テーブル
EXPECTED = {"topics", "queries", "papers", "screening", "sections", "chunks",
            "chunks_fts", "extractions", "taxonomy", "paper_tags", "runs"}


def test_schema_has_all_tables():
    # メモリDBに作って、SPEC 記載のテーブルが全部あるか確認
    conn = init_db(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    assert EXPECTED <= names


def test_init_is_idempotent():
    # 2回初期化してもエラーにならない
    conn = init_db(":memory:")
    conn.executescript(open("app/schema.sql").read())


def test_fts5_works():
    # FTS5 で実際にキーワード検索できる(RAG の前提)
    conn = init_db(":memory:")
    conn.execute("INSERT INTO papers(id, title) VALUES (1,'t')")
    conn.execute("INSERT INTO chunks(id, paper_id, text) VALUES (1,1,'CRISPR knockout screening')")
    conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (1,'CRISPR knockout screening')")
    hit = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'crispr'").fetchall()
    assert len(hit) == 1


def test_faiss_importable():
    # ベクトル検索ライブラリが入っている
    import faiss
    assert faiss.IndexFlatL2(4).d == 4


def test_config_has_models():
    # モデル名が config から読める
    cfg = load_config()
    assert cfg["models"]["llm"] and cfg["models"]["embedding"]


def test_health_endpoint(tmp_path, monkeypatch):
    # /health が 200 を返し、テーブル数とモデル名を含む
    monkeypatch.setattr("app.db.DB_PATH", str(tmp_path / "t.sqlite"))
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["tables"] >= len(EXPECTED)
