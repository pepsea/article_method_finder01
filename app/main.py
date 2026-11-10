"""FastAPI アプリ本体: health、検索履歴、判定の確認・上書き、画面(web/)の配信。"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import export
from app.config import load_config
from app.db import init_db

app = FastAPI(title="Method Discovery from Literature")


@app.get("/health")
def health():
    # DBが開けるか・テーブルが揃っているか・使うモデル名を返して、動作確認に使う
    conn = init_db()
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    cfg = load_config()
    return {"status": "ok", "tables": len(tables), "models": cfg["models"]}


@app.get("/api/searches")
def api_searches():
    # 履歴ページ: 検索(トピック)一覧と、各々の保存済みスナップショット
    conn = init_db()
    try:
        return export.list_searches(conn)
    finally:
        conn.close()


@app.post("/api/searches/{topic_id}/export")
def api_export(topic_id: int):
    # 現在の結果を新しい日時フォルダに保存する(履歴に追加される)
    conn = init_db()
    try:
        if not conn.execute("SELECT 1 FROM topics WHERE id=?", (topic_id,)).fetchone():
            raise HTTPException(404, "topic not found")
        return {"folder": str(export.export_search(conn, topic_id))}
    finally:
        conn.close()


@app.get("/api/searches/{topic_id}/papers")
def api_papers(topic_id: int, label: str = "", q: str = "", limit: int = 500):
    # 論文一覧(判定ラベルと理由つき)。label と題名の部分一致で絞り込める
    conn = init_db()
    try:
        rows = [dict(r) for r in conn.execute(export.PAPER_SQL, (topic_id, topic_id))]
    finally:
        conn.close()
    if label == "none":
        rows = [r for r in rows if r["label"] is None]
    elif label:
        rows = [r for r in rows if r["label"] == label]
    if q:
        rows = [r for r in rows if q.lower() in (r["title"] or "").lower()]
    return {"total": len(rows), "papers": rows[:limit]}


class Override(BaseModel):
    paper_id: int
    topic_id: int
    label: str
    reason: str = "manual override"


@app.post("/api/screening/override")
def api_override(o: Override):
    # 人の判断で判定を上書きする(reviewer=human になり、以後のLLM再実行で消えない)
    if o.label not in ("include", "exclude", "unsure"):
        raise HTTPException(400, "label must be include/exclude/unsure")
    conn = init_db()
    try:
        conn.execute(
            "INSERT INTO screening(paper_id, topic_id, label, reason, reviewer) VALUES (?,?,?,?, 'human') "
            "ON CONFLICT(paper_id, topic_id) DO UPDATE SET label=excluded.label, reason=excluded.reason, "
            "reviewer='human', updated_at=CURRENT_TIMESTAMP", (o.paper_id, o.topic_id, o.label, o.reason))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# 保存済みフォルダ(CSV等)と画面を配信する。"/" は最後に登録する
export.SEARCH_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/searches", StaticFiles(directory=str(export.SEARCH_DIR)), name="searches")
if Path("web").exists():
    app.mount("/", StaticFiles(directory="web", html=True), name="web")
