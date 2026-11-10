"""ステージH: 手法の分類体系。プロファイルの規則から体系を作り、「other」に残った論文はクラスタリングとLLMで新しい分類を提案し、人が確認して採用する。採用した体系は保存され、再実行しても同じタグになる。"""
import json
import re

import httpx
import numpy as np

from app import knowledge, profile as prof, rag
from app.config import load_config


def tag_papers(conn, topic_id):
    # 体系(taxonomy)テーブルを現在のプロファイルの手法クラスに合わせ、使える論文にタグを付ける(規則ベースなので決定的)
    P = prof.load(conn, topic_id)
    ids = {}
    for name, regex in P["method_classes"] + [["other", ""]]:
        row = conn.execute("SELECT id FROM taxonomy WHERE topic_id=? AND name=?", (topic_id, name)).fetchone()
        if row:
            conn.execute("UPDATE taxonomy SET definition=? WHERE id=?", (regex, row["id"]))
            ids[name] = row["id"]
        else:
            ids[name] = conn.execute("INSERT INTO taxonomy(topic_id, name, definition) VALUES (?,?,?)", (topic_id, name, regex)).lastrowid
    conn.execute("DELETE FROM paper_tags WHERE reviewer='rule' AND taxonomy_id IN (SELECT id FROM taxonomy WHERE topic_id=?)", (topic_id,))
    n = 0
    for it in knowledge.load_knowledge(conn, topic_id):
        if it["prep"]:
            pid = conn.execute("SELECT id FROM papers WHERE pmid=?", (it["pmid"],)).fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO paper_tags(paper_id, taxonomy_id, confidence, reviewer) VALUES (?,?,?,'rule')", (pid, ids[it["prep"]], 1.0))
            n += 1
    conn.commit()
    return {"classes": len(ids), "tagged": n}


def propose(conn, topic_id, max_clusters=6, embed_fn=rag.embed, ask_fn=None):
    # 「other」に分類された論文の手法の記述を埋め込み→FAISSのk-meansでまとめ、各グループにLLMが名前・正規表現・定義を提案する(人が確認するための案)
    import faiss
    cfg = load_config()
    items = [i for i in knowledge.load_knowledge(conn, topic_id) if i["prep"] == "other"]
    texts = [i["f"]["sample_preparation"]["value"] for i in items]
    if len(texts) < 4:
        return []
    k = max(2, min(max_clusters, len(texts) // 8))
    X = embed_fn(texts, cfg)
    km = faiss.Kmeans(X.shape[1], k, niter=25, seed=0)
    km.train(X)
    _, assign = km.index.search(X, 1)
    out = []
    for c in range(k):
        members = [texts[i] for i in range(len(texts)) if assign[i][0] == c]
        if not members:
            continue
        ex = members[:12]
        prompt = ("These method descriptions from papers were not matched by any existing method class. Propose ONE new class that covers most of them. "
                  'Return JSON {"name": "<short class name>", "regex": "<case-insensitive regex of lower-case word stems that match the descriptions>", "definition": "<one sentence>"}.\n\n'
                  + "\n".join(f"- {m[:160]}" for m in ex))
        if ask_fn:
            d = ask_fn(prompt)
        else:
            r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=600, json={
                "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False, "format": "json",
                "options": {"temperature": 0, "seed": 0, "num_ctx": 4096, "num_predict": 300}})
            r.raise_for_status()
            try:
                d = json.loads(r.json()["response"])
            except json.JSONDecodeError:
                continue
        try:
            rx = re.compile(d["regex"], re.I)
        except (re.error, KeyError, TypeError):
            continue
        cov = sum(bool(rx.search(m)) for m in members) / len(members)
        out.append({"name": d.get("name", f"cluster {c}"), "regex": d["regex"], "definition": d.get("definition", ""),
                    "n_papers": len(members), "regex_coverage": round(cov, 2), "examples": [m[:100] for m in members[:3]], "accept": cov >= 0.5})
    return out


def apply(conn, topic_id, proposals):
    # 人が確認した案(accept=True のもの)を、プロファイルの手法クラスに追加して保存し、タグを付け直す。体系はプロファイルに残るので再実行しても変わらない
    P = prof.load(conn, topic_id)
    names = {n for n, _ in P["method_classes"]}
    added = []
    for p in proposals:
        if p.get("accept") and p["name"] not in names:
            P["method_classes"].append([p["name"], p["regex"]])
            names.add(p["name"])
            added.append(p["name"])
    errs = prof.validate(P)
    if errs:
        raise ValueError(errs)
    prof.save(conn, topic_id, P)
    return {"added": added, **tag_papers(conn, topic_id)}
