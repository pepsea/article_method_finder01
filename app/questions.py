"""質問から検索を作る: 自然な文の研究質問 → PubMed検索式(件数で検証) → トピック作成、または知識が足りない質問に答えるための追加収集。"""
import json
import re

import httpx

from app import fulltext, knowledge, profile as prof, pubmed, rag, stages
from app.config import load_config

MIN_HITS = 5   # これ未満の検索式は狭すぎて使わない(上限は config の max_hits_per_query)


STOP = {"what", "which", "that", "this", "with", "from", "using", "used", "best", "does", "have", "into", "than", "about", "how", "for", "and", "the",
        "are", "was", "were", "its", "can", "should", "when", "where", "who", "why", "method", "methods", "analysis"}


def keywords_of(question):
    # 質問の中身の語(ストップワードを除く、4文字以上)。判定の優先順位づけに使う
    return [w for w in dict.fromkeys(re.findall(r"[a-z][a-z0-9\-]{3,}", question.lower())) if w not in STOP]


def anchor(P):
    # トピックの対象語(entity_regex)から PubMed の絞り込み "(stem* OR stem* ...)" を作る。単純な語幹だけを使い、無ければ None
    stems = [t.strip() for t in P["entity_regex"].split("|") if re.fullmatch(r"[A-Za-z0-9\- ]{4,}", t.strip())]
    return "(" + " OR ".join(f"{s}*" for s in stems) + ")" if stems else None


def queries_from_question(question, n=6, context=None, anchor_terms=None):
    # 質問からLLMが PubMed 検索式を作り、PubMedの件数で検証する。件数が 5 〜 上限 の式だけを seeds として返す。
    # anchor_terms があれば、全ての式に AND で付ける(質問が既存トピックの対象から外れた論文を呼ばないように)
    cfg = load_config()
    hint = f" The existing topic is: {context}." if context else ""
    prompt = (f"A researcher asks: \"{question}\"{hint}\n"
              f"Write {n} PubMed search queries (boolean strings with AND/OR and parentheses) that would find papers able to answer it. "
              "Cover the key concepts (subject/compound, method, sample or condition) with synonyms and abbreviations. Order them from specific to broad. "
              'Return JSON only: {"topic_name": "<short title of at most 8 words>", "queries": ["...", ...]}')
    r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=600, json={
        "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 4096, "num_predict": 600}})
    r.raise_for_status()
    d = json.loads(r.json()["response"])
    rows = []
    for q in dict.fromkeys(x.strip() for x in d.get("queries", []) if isinstance(x, str) and x.strip()):
        if anchor_terms:
            q = f"({q}) AND {anchor_terms}"
        count = pubmed.search(q, cfg["search_end_date"])["count"]
        status = "ok" if MIN_HITS <= count <= cfg["max_hits_per_query"] else ("too narrow" if count < MIN_HITS else "too broad")
        rows.append({"query": q, "count": count, "status": status})
    return {"topic_name": str(d.get("topic_name") or question[:60]).strip(), "queries": rows,
            "seeds": [r["query"] for r in rows if r["status"] == "ok"]}


def topic_from_question(conn, question):
    # 質問からトピックを作る: 検索式を作って検証し、トピック(プロファイル・基準の下書きも)を登録する。(topic_id, 検索式の一覧) を返す
    res = queries_from_question(question)
    if not res["seeds"]:
        raise ValueError("no usable PubMed query (all too narrow or too broad): " + json.dumps(res["queries"]))
    return stages.setup_topic(conn, res["topic_name"], res["seeds"]), res


def expand_for_question(conn, topic_id, question, screen_limit=100, extract_limit=30):
    # 質問に答えるための追加収集: 質問から新しい検索式を作って登録し、収集 → 判定 → 全文 → 抽出 → 索引 まで進める(知識は蓄積される)
    name = conn.execute("SELECT name FROM topics WHERE id=?", (topic_id,)).fetchone()["name"]
    res = queries_from_question(question, context=name, anchor_terms=anchor(prof.load(conn, topic_id)))
    from app import querydesign   # 質問から作った式も、設計と同じ関門を通す(関連率・重複・上限)
    added = querydesign.gate(conn, topic_id, [q["query"] for q in res["queries"] if q["status"] == "ok"], origin="snowball")
    coll = stages.collect(conn, topic_id)
    stages.dedup(conn)
    scr = stages.screen(conn, topic_id, limit=screen_limit, boost=keywords_of(question))
    ft = fulltext.fetch_all(conn, topic_id)
    ext = knowledge.extract_all(conn, topic_id, extract_limit)
    idx = rag.build_index(conn, topic_id)
    return {"new_queries": added, "new_papers": coll["added"], "screening": scr, "fulltext": ft, "extraction": ext, "index": idx}


def ask_with_expansion(conn, topic_id, question, expand="auto", screen_limit=100, extract_limit=30):
    # 質問に答える。根拠(検証済みの引用)が無いとき(expand="auto")、または常に(expand=True)、質問から追加収集して答え直す
    first = rag.ask(conn, topic_id, question)
    if expand is False or (expand == "auto" and any(e["verified"] for e in first["evidence"])):
        return {"expanded": None, "first": first, "final": first}
    stats = expand_for_question(conn, topic_id, question, screen_limit, extract_limit)
    return {"expanded": stats, "first": first, "final": rag.ask(conn, topic_id, question)}
