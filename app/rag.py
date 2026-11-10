"""ステージG: 検索基盤(RAG)。採用論文の要旨と全文をチャンクに分け、キーワード検索(SQLite FTS5)とベクトル検索(FAISS)を合わせて検索し、引用つきで質問に答える。"""
import json
import re

import httpx
import numpy as np

from app import stages
from app.config import load_config

WORDS = 260      # 1チャンクの語数(約340トークン。SPECの300〜500トークン)
OVERLAP = 40     # 前後のチャンクと重ねる語数(節をまたいでは重ねない)


def embed(texts, cfg=None):
    # Ollama の埋め込みモデル(config の models.embedding)でベクトル化し、長さ1に正規化して返す(内積=コサイン類似度)
    cfg = cfg or load_config()
    out = []
    for i in range(0, len(texts), 16):
        r = httpx.post(f"{cfg['ollama_url']}/api/embed", timeout=300,
                       json={"model": cfg["models"]["embedding"], "input": texts[i:i + 16]})
        r.raise_for_status()
        out += r.json()["embeddings"]
    a = np.array(out, dtype="float32")
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def split(text):
    # 1つの節を、WORDS語ごと(OVERLAP語重ねて)のチャンクに分ける
    w = text.split()
    if len(w) <= WORDS:
        return [" ".join(w)] if w else []
    step = WORDS - OVERLAP
    return [" ".join(w[i:i + WORDS]) for i in range(0, len(w) - OVERLAP, step)]


def build_index(conn, topic_id, embed_fn=embed):
    # 採用論文の要旨と全文の節をチャンク化し、FTS5と埋め込みに登録する(再開可能)。全文が後から手に入った論文は作り直す
    papers = conn.execute("SELECT p.* FROM papers p JOIN screening s ON s.paper_id=p.id AND s.topic_id=? WHERE s.label='include'", (topic_id,)).fetchall()
    new_chunks = 0
    for p in papers:
        has_sec = conn.execute("SELECT 1 FROM sections WHERE paper_id=?", (p["id"],)).fetchone()
        has_sec_chunks = conn.execute("SELECT 1 FROM chunks WHERE paper_id=? AND section_id IS NOT NULL", (p["id"],)).fetchone()
        has_any = conn.execute("SELECT 1 FROM chunks WHERE paper_id=?", (p["id"],)).fetchone()
        if has_any and not (has_sec and not has_sec_chunks):
            continue
        for c in conn.execute("SELECT id, text FROM chunks WHERE paper_id=?", (p["id"],)).fetchall():   # 作り直す前に古いものを消す
            conn.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", (c["id"], c["text"]))
            conn.execute("DELETE FROM chunk_vectors WHERE chunk_id=?", (c["id"],))
        conn.execute("DELETE FROM chunks WHERE paper_id=?", (p["id"],))
        pieces = [(None, t) for t in split((p["title"] or "") + ". " + (p["abstract"] or ""))]
        for s in conn.execute("SELECT id, text FROM sections WHERE paper_id=?", (p["id"],)):
            pieces += [(s["id"], t) for t in split(s["text"])]
        for sec_id, t in pieces:
            cur = conn.execute("INSERT INTO chunks(paper_id, section_id, text, token_count) VALUES (?,?,?,?)",
                               (p["id"], sec_id, t, int(len(t.split()) * 1.3)))
            conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?,?)", (cur.lastrowid, t))
            new_chunks += 1
    conn.commit()
    # ベクトルが未計算のチャンクを埋め込む
    todo = conn.execute("SELECT c.id, c.text FROM chunks c JOIN screening s ON s.paper_id=c.paper_id AND s.topic_id=? AND s.label='include' "
                        "WHERE NOT EXISTS (SELECT 1 FROM chunk_vectors v WHERE v.chunk_id=c.id)", (topic_id,)).fetchall()
    cfg = load_config()
    for i in range(0, len(todo), 64):
        batch = todo[i:i + 64]
        vecs = embed_fn([b["text"] for b in batch], cfg)
        for b, v in zip(batch, vecs):
            conn.execute("INSERT INTO chunk_vectors(chunk_id, model, dim, vec) VALUES (?,?,?,?)", (b["id"], cfg["models"]["embedding"], len(v), v.tobytes()))
        conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM chunks c JOIN screening s ON s.paper_id=c.paper_id AND s.topic_id=? AND s.label='include'", (topic_id,)).fetchone()[0]
    return {"papers": len(papers), "new_chunks": new_chunks, "embedded": len(todo), "chunks_total": n}


def _eligible(conn, topic_id, year_min, year_max, preprint):
    # フィルタ(年・プレプリントか)を満たす、このトピックのチャンクID集合と情報
    sql = ("SELECT c.id, c.paper_id, c.text, c.section_id, p.pmid, p.year, p.title FROM chunks c JOIN papers p ON p.id=c.paper_id "
           "JOIN screening s ON s.paper_id=p.id AND s.topic_id=? AND s.label='include' WHERE 1=1")
    args = [topic_id]
    if year_min: sql += " AND p.year>=?"; args.append(year_min)
    if year_max: sql += " AND p.year<=?"; args.append(year_max)
    if preprint is not None: sql += " AND p.is_preprint=?"; args.append(int(preprint))
    return {r["id"]: r for r in conn.execute(sql, args)}


def keyword_search(conn, ids, query, n=50):
    # FTS5(BM25)による語の検索。クエリは語ごとのOR。適格なチャンクだけを順位順に返す
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", query) if len(t) > 2]
    if not terms:
        return []
    q = " OR ".join('"%s"' % t for t in terms)
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?", (q, n * 6)).fetchall()
    return [r[0] for r in rows if r[0] in ids][:n]


def vector_search(conn, ids, query, n=50, embed_fn=embed):
    # FAISS(内積)によるベクトル検索。適格なチャンクのベクトルでインデックスを作って検索する
    import faiss
    rows = conn.execute("SELECT chunk_id, dim, vec FROM chunk_vectors WHERE chunk_id IN (%s)" % ",".join(map(str, ids or [0]))).fetchall() if ids else []
    if not rows:
        return []
    mat = np.vstack([np.frombuffer(r["vec"], dtype="float32") for r in rows])
    index = faiss.IndexIDMap(faiss.IndexFlatIP(mat.shape[1]))
    index.add_with_ids(mat, np.array([r["chunk_id"] for r in rows], dtype="int64"))
    _, found = index.search(embed_fn([query])[:1], min(n, len(rows)))
    return [int(i) for i in found[0] if i != -1]


def search(conn, topic_id, query, k=8, year_min=None, year_max=None, preprint=None, mode="hybrid", embed_fn=embed):
    # ハイブリッド検索: キーワード順位とベクトル順位を RRF(逆順位融合)で合わせる。mode で keyword / vector のみも選べる
    ids = _eligible(conn, topic_id, year_min, year_max, preprint)
    kw = keyword_search(conn, ids, query) if mode in ("hybrid", "keyword") else []
    vec = vector_search(conn, ids, query, embed_fn=embed_fn) if mode in ("hybrid", "vector") else []
    score = {}
    for ranking in (kw, vec):
        for r, cid in enumerate(ranking):
            score[cid] = score.get(cid, 0) + 1 / (60 + r + 1)
    out = []
    for cid in sorted(score, key=score.get, reverse=True)[:k]:
        r = ids[cid]
        sec = conn.execute("SELECT name FROM sections WHERE id=?", (r["section_id"],)).fetchone() if r["section_id"] else None
        out.append({"chunk_id": cid, "pmid": r["pmid"], "year": r["year"], "title": r["title"], "section": sec[0] if sec else "Abstract",
                    "text": r["text"], "score": round(score[cid], 4), "via": "both" if cid in kw and cid in vec else ("keyword" if cid in kw else "vector")})
    return out


def ask(conn, topic_id, question, k=6, **filters):
    # 質問に答える: 検索した抜粋だけを根拠にLLMが答え、根拠の引用文が抜粋に実在するかを検証して付ける(捏造を防ぐ)
    cfg = load_config()
    chunks = search(conn, topic_id, question, k=k, **filters)
    if not chunks:
        return {"answer": "No relevant passages found.", "evidence": [], "chunks": []}
    ctx = "\n\n".join(f"[{i}] (PMID {c['pmid']}, {c['year']}, {c['section']})\n{c['text']}" for i, c in enumerate(chunks, 1))
    prompt = ("Answer the question using ONLY the numbered excerpts. Cite excerpt numbers like [1]. If they do not contain the answer, say so. "
              'For each key claim give an EXACT quote (at most 150 characters) copied from the cited excerpt. Return JSON: {"answer": "...", "evidence": [{"n": 1, "quote": "..."}]}.\n\n'
              f"QUESTION: {question}\n\nEXCERPTS:\n{ctx}")
    r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=900, json={
        "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 700}})
    r.raise_for_status()
    from app.knowledge import contains
    try:
        out = json.loads(r.json()["response"])
    except json.JSONDecodeError:
        out = {"answer": "(model returned invalid JSON)", "evidence": []}
    ev = []
    for e in out.get("evidence", []) if isinstance(out.get("evidence"), list) else []:
        try:
            c = chunks[int(e.get("n")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        ev.append({"n": e["n"], "pmid": c["pmid"], "quote": e.get("quote"), "verified": contains(str(e.get("quote") or ""), c["text"])})
    return {"answer": out.get("answer", ""), "evidence": ev, "chunks": chunks}


def evaluate(conn, topic_id, cases, k=5, embed_fn=embed):
    # 検索の評価(M5): (質問, 期待するPMID) の一覧で、keyword / vector / hybrid の上位k件に期待PMIDが入った割合を比べる
    res = {m: 0 for m in ("keyword", "vector", "hybrid")}
    for q, pmid in cases:
        for m in res:
            res[m] += any(c["pmid"] == str(pmid) for c in search(conn, topic_id, q, k=k, mode=m, embed_fn=embed_fn))
    return {m: f"{v}/{len(cases)}" for m, v in res.items()}
