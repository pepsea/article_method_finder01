"""SQLite の接続とスキーマ作成。"""
import os
import sqlite3
from pathlib import Path

# DBファイルの場所。環境変数 DB_PATH で変更でき、無ければ data/db/app.sqlite を使う
DB_PATH = os.environ.get("DB_PATH", "data/db/app.sqlite")
SCHEMA_FILE = Path(__file__).parent / "schema.sql"


def connect(path=None):
    # DB接続を返す。外部キー制約を有効にして、行を辞書風に読めるようにする
    path = path or DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=None):
    # schema.sql を実行して全テーブルを作る(何度呼んでも安全)
    conn = connect(path)
    conn.executescript(SCHEMA_FILE.read_text())
    # 旧DBの移行: topics に profile 列が無ければ追加し、判定済みの論文は topic_papers に紐づける
    cols = [r[1] for r in conn.execute("PRAGMA table_info(topics)")]
    if "profile" not in cols:
        import json
        from app import profile as prof
        conn.execute("ALTER TABLE topics ADD COLUMN profile TEXT")
        conn.execute("UPDATE topics SET profile=?", (json.dumps(prof.NICOTINE, ensure_ascii=False),))
    # 旧DBの移行: queries に決定記録の列(status: kept/dropped、precision、novelty、reason)を追加する。NULL は「使う」と同じ扱い
    qcols = [r[1] for r in conn.execute("PRAGMA table_info(queries)")]
    for col, typ in (("status", "TEXT"), ("precision", "REAL"), ("novelty", "REAL"), ("reason", "TEXT")):
        if col not in qcols:
            conn.execute(f"ALTER TABLE queries ADD COLUMN {col} {typ}")
    conn.execute("INSERT OR IGNORE INTO topic_papers(topic_id, paper_id) SELECT topic_id, paper_id FROM screening")
    # 全文の試行記録は、表が空のとき(旧DBからの初回移行)だけ作る。毎回作ると、削除した記録が復活してしまう
    if conn.execute("SELECT COUNT(*) FROM fulltext").fetchone()[0] == 0:
        conn.execute("INSERT OR IGNORE INTO fulltext(paper_id, source, status, licence, updated_at) "
                     "SELECT p.id, 'pmc', CASE WHEN EXISTS (SELECT 1 FROM sections s WHERE s.paper_id=p.id) THEN 'ok' ELSE 'licence_not_usable' END, "
                     "p.licence, '2000-01-01 00:00:00' FROM papers p WHERE p.licence IS NOT NULL")
    conn.commit()
    return conn
