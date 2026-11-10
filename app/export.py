"""検索ごとのフォルダ出力。searches/<topic_id>_<名前>/<日時>/ に、その時点の結果を保存する。"""
import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path

SEARCH_DIR = Path(os.environ.get("SEARCH_DIR", "searches"))

PAPER_SQL = """
SELECT p.pmid, p.doi, p.year, p.title, p.journal, p.url, p.status,
       s.label, s.reviewer, s.reason, p.id AS paper_id
FROM papers p JOIN topic_papers tp ON tp.paper_id=p.id AND tp.topic_id=?
LEFT JOIN screening s ON s.paper_id=p.id AND s.topic_id=?
WHERE p.primary_paper_id IS NULL ORDER BY p.year DESC, p.pmid
"""


def slug(name):
    # フォルダ名に使える短い名前にする(英数字とハイフンのみ)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def topic_dir(topic_id, name):
    # トピック(=1つの検索テーマ)ごとの親フォルダ
    return SEARCH_DIR / f"{topic_id}_{slug(name)}"


def counts(conn, topic_id):
    # 現時点の件数をまとめる(PRISMA風: 収集→重複除外→判定結果)
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    out = {
        "queries": q("SELECT COUNT(*) FROM queries WHERE topic_id=?", topic_id),
        "collected": q("SELECT COUNT(*) FROM topic_papers WHERE topic_id=?", topic_id),
        "duplicates_merged": q("SELECT COUNT(*) FROM papers p JOIN topic_papers tp ON tp.paper_id=p.id AND tp.topic_id=? WHERE p.primary_paper_id IS NOT NULL", topic_id),
        "retracted": q("SELECT COUNT(*) FROM papers p JOIN topic_papers tp ON tp.paper_id=p.id AND tp.topic_id=? WHERE p.is_retracted=1", topic_id),
    }
    for label in ("include", "exclude", "unsure"):
        out[label] = q("SELECT COUNT(*) FROM screening WHERE topic_id=? AND label=?", topic_id, label)
    out["not_screened"] = out["collected"] - out["duplicates_merged"] - out["retracted"] \
        - out["include"] - out["exclude"] - out["unsure"]
    return out


def export_search(conn, topic_id):
    # 現在の結果を新しい日時フォルダに保存し、そのパスを返す(過去の保存は消さない)
    t = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = topic_dir(topic_id, t["name"]) / stamp
    d.mkdir(parents=True)
    rows = [dict(r) for r in conn.execute(PAPER_SQL, (topic_id, topic_id))]
    cols = ["pmid", "doi", "year", "title", "journal", "label", "reviewer", "reason", "url"]
    for fname, subset in (("papers.csv", rows), ("included.csv", [r for r in rows if r["label"] == "include"]),
                          ("review_unsure.csv", [r for r in rows if r["label"] == "unsure"])):
        with (d / fname).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(subset)
    with (d / "queries.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["term", "origin", "hit_count", "status", "precision", "novelty", "reason"])
        w.writerows(conn.execute("SELECT term, origin, hit_count, status, precision, novelty, reason FROM queries WHERE topic_id=?", (topic_id,)))
    runs = [dict(r) for r in conn.execute(
        "SELECT id, stage, started_at, model, model_version, prompt_version, notes FROM runs ORDER BY id")]
    (d / "criteria.txt").write_text(t["criteria"] or "")
    summary = {"topic_id": topic_id, "topic": t["name"], "saved_at": stamp, "counts": counts(conn, topic_id), "runs": runs}
    (d / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return d


def list_searches(conn):
    # 履歴ページ用: 全トピックと、それぞれの保存済みスナップショット一覧
    out = []
    for t in conn.execute("SELECT * FROM topics ORDER BY id DESC"):
        base = topic_dir(t["id"], t["name"])
        snaps = sorted([p.name for p in base.iterdir() if (p / "summary.json").exists()], reverse=True) if base.exists() else []
        out.append({"id": t["id"], "name": t["name"], "created_at": t["created_at"],
                    "counts": counts(conn, t["id"]), "snapshots": snaps, "folder": base.name})
    return out
