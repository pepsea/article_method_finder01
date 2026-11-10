"""ステージA: 検索語の設計と決定のワークフロー。
  1. 定義(define): 研究質問 → 対象・手法・条件・除外の語をLLMが整理(人が編集できる)
  2. 候補(candidates): 定義から体系的に組み立てた式 + MeSH + LLMの自由な式
  3. 試験(pilot): 各候補の結果からPubMedの論文を無作為に取り、トピックの基準で関連率(precision)と、既に選んだ式がカバーしていない割合(novelty)を測る
  4. 決定(decide): 基準を満たす式だけを貪欲に選ぶ。選ばなかった式にも理由を残す
  5. 確認(override): 人が採用/不採用を上書きできる
  6. 以後の追加(知識駆動ループ・質問による追加収集)の式も、同じ関門(gate)を通す
どの式も「なぜ使う/使わないか」が queries テーブルに残る。トピックの内容はコードに書かれていない。"""
import json
import random
import re

import httpx

from app import profile as prof, pubmed, stages
from app.config import load_config

DEFAULTS = {"pilot_n": 12, "min_precision": 0.3, "min_novelty": 0.2, "max_total_papers": 4000, "min_hits": 5, "max_candidates": 12}


def settings():
    # 設計の基準(config.yaml の query_design。無い項目は既定値)
    return {**DEFAULTS, **(load_config().get("query_design") or {})}


# ---------- 1. 定義 ----------

def _terms(v, n=8):
    # LLMの出力を、文字列のリスト(最大n個、重複なし)にそろえる
    return list(dict.fromkeys(str(x).strip() for x in v if str(x).strip()))[:n] if isinstance(v, list) else []


def define(question):
    # 研究質問から、検索の範囲(対象・手法・条件・除外)を構造化して返す。人が確認・編集してから次に進む
    cfg = load_config()
    prompt = (f'A researcher wants to find literature to answer: "{question}"\nDefine the search scope as JSON: '
              '{"topic_name": "<short title, at most 8 words>", "goal": "<one sentence: the decision the review should support>", '
              '"subject_terms": ["<the main subject entities with synonyms and abbreviations, e.g. a compound, disease, organism or system>"], '
              '"method_terms": ["<methods, techniques or approaches of interest, with synonyms>"], '
              '"condition_terms": ["<sample types, populations, settings or conditions to compare, if any>"], '
              '"exclude_terms": ["<topics to avoid, if any>"]}. '
              "Use 2-8 items per list (empty list if not applicable). Items must work as PubMed search words, without boolean operators.")
    r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=600, json={
        "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 4096, "num_predict": 700}})
    r.raise_for_status()
    d = json.loads(r.json()["response"])
    out = {"question": question, "topic_name": str(d.get("topic_name") or question[:60]).strip(), "goal": str(d.get("goal") or "").strip(),
           "subject_terms": _terms(d.get("subject_terms")), "method_terms": _terms(d.get("method_terms")),
           "condition_terms": _terms(d.get("condition_terms"), 6), "exclude_terms": _terms(d.get("exclude_terms"), 4)}
    if not out["subject_terms"]:
        raise ValueError("the model returned no subject terms; rephrase the question")
    return out


# ---------- 2. 候補 ----------

def orx(terms):
    # 語のリストを "(a OR "b c" OR d)" の形にする(空白や / を含む語は引用符で囲む)
    return "(" + " OR ".join(f'"{t}"' if re.search(r"[\s/]", t) else t for t in terms) + ")"


def candidates(defn, with_mesh=True, with_llm=True):
    # 定義から検索式の候補を作る: 全語の組(core)・最も狭い組・条件ごとの式・MeSH・LLMの自由な式。origin と理由を付ける
    S, M, C, X = defn["subject_terms"], defn["method_terms"], defn["condition_terms"], defn["exclude_terms"]
    base = f"{orx(S)} AND {orx(M)}" if M else orx(S)
    notx = f" AND NOT {orx(X)}" if X else ""
    out = [{"query": base + notx, "origin": "seed", "why": "core: all subject terms x all method terms"}]
    if len(S) > 1 or len(M) > 1:
        narrow = f"{orx(S[:1])}" + (f" AND {orx(M[:1])}" if M else "")
        out.append({"query": narrow + notx, "origin": "seed", "why": "narrow: first subject term x first method term"})
    for c in C[:5]:
        out.append({"query": f"{base} AND {orx([c])}" + notx, "origin": "seed", "why": f"condition: {c}"})
    if with_mesh:
        try:
            tr = pubmed.search(base, load_config()["search_end_date"])["translation"]
            for m in list(dict.fromkeys(re.findall(r'"([^"]+)"\[MeSH Terms\]', tr)))[:3]:
                out.append({"query": f'"{m}"[MeSH Terms]' + (f" AND {orx(M)}" if M else "") + notx, "origin": "mesh", "why": f"MeSH term matched by PubMed: {m}"})
        except Exception:
            pass
    if with_llm:
        from app import questions
        try:
            for q in questions.queries_from_question(defn["question"])["queries"]:
                out.append({"query": q["query"] + notx, "origin": "llm", "why": "free-form query written by the model from the question"})
        except Exception:
            pass
    seen, uniq = set(), []
    for c in out:
        if c["query"].lower() not in seen:
            seen.add(c["query"].lower())
            uniq.append(c)
    return uniq[:settings()["max_candidates"]]


# ---------- 3. 試験(pilot) ----------

def default_judge(conn, topic_id):
    # トピックの基準で1本を include/exclude/unsure に判定する関数を返す(判定にはスクリーニングと同じモデルとプロンプトを使う)
    cfg = load_config()
    cfg["models"]["llm"] = cfg.get("screening_model") or cfg["models"]["llm"]
    criteria = conn.execute("SELECT criteria FROM topics WHERE id=?", (topic_id,)).fetchone()["criteria"]
    ent = prof.rx(prof.load(conn, topic_id), "entity_regex")

    def judge(p):
        if p["abstract"] and not ent.search((p["title"] or "") + " " + p["abstract"]):
            return "exclude"      # トピックの語が無ければLLMを使わず除外(スクリーニングと同じ規則)
        return stages._screen_one(cfg, criteria, p)[0]
    return judge


def evaluate(cand, judge, st=None):
    # 候補1つを評価する: 件数 → (範囲内なら)全IDを取得 → 無作為サンプルを判定して precision を出す。結果は検索の応答も論文もキャッシュされる
    st = st or settings()
    cfg = load_config()
    q = cand["query"]
    res = pubmed.search(q, cfg["search_end_date"])
    row = {**cand, "count": res["count"], "ids": [], "precision": None, "sampled": 0, "included": 0, "status": None, "reason": None}
    if res["count"] < st["min_hits"]:
        return {**row, "status": "dropped", "reason": f"too narrow: {res['count']} hits (< {st['min_hits']})"}
    if res["count"] > cfg["max_hits_per_query"]:
        return {**row, "status": "dropped", "reason": f"too broad: {res['count']} hits (> {cfg['max_hits_per_query']})"}
    ids = pubmed.search(q, cfg["search_end_date"], retmax=res["count"])["ids"]
    sample = random.Random(0).sample(sorted(ids), min(st["pilot_n"], len(ids)))   # 固定seedなので再実行で同じ
    papers = pubmed.fetch(sample)
    inc = sum(judge(p) == "include" for p in papers)
    return {**row, "ids": ids, "sampled": len(papers), "included": inc, "precision": round(inc / len(papers), 2) if papers else 0.0}


# ---------- 4. 決定 ----------

def decide(rows, union=None, st=None):
    # 貪欲に選ぶ: 関連率の高い順に見て、基準(precision・novelty・合計の上限)を満たす式を採用する。全ての行に status と reason を付けて返す
    st = st or settings()
    union = set(union or ())
    for r in sorted((r for r in rows if r["status"] is None), key=lambda r: (-(r["precision"] or 0), r["count"])):
        ids = set(r["ids"])
        r["novelty"] = round(len(ids - union) / len(ids), 2) if ids else 0.0
        if r["precision"] < st["min_precision"]:
            r["status"], r["reason"] = "dropped", f"low relevance: {r['included']}/{r['sampled']} sampled papers relevant ({r['precision']:.0%} < {st['min_precision']:.0%})"
        elif r["novelty"] < st["min_novelty"]:
            r["status"], r["reason"] = "dropped", f"redundant: {1 - r['novelty']:.0%} of its papers are already covered by chosen queries"
        elif len(union | ids) > st["max_total_papers"]:
            r["status"], r["reason"] = "dropped", f"budget: would exceed {st['max_total_papers']} papers in total"
        else:
            union |= ids
            r["status"], r["reason"] = "kept", f"{r['included']}/{r['sampled']} sampled relevant ({r['precision']:.0%}), {r['novelty']:.0%} new papers"
    return rows


def union_of_kept(conn, topic_id):
    # このトピックで既に採用している式が集める論文IDの和集合(キャッシュ済みの検索から)
    end = load_config()["search_end_date"]
    cap = load_config()["max_hits_per_query"]
    u = set()
    for r in conn.execute("SELECT term, hit_count FROM queries WHERE topic_id=? AND hit_count>0 AND (status IS NULL OR status='kept')", (topic_id,)):
        if r["hit_count"] <= cap:
            u |= set(pubmed.search(r["term"], end, retmax=r["hit_count"])["ids"])
    return u


def register(conn, topic_id, rows, run):
    # 決定を queries に保存する(採用も不採用も、理由つきで)。同じ式が既にあれば何もしない
    n = 0
    for r in rows:
        if conn.execute("SELECT 1 FROM queries WHERE topic_id=? AND lower(term)=lower(?)", (topic_id, r["query"])).fetchone():
            continue
        conn.execute("INSERT INTO queries(topic_id, term, origin, source, run_id, hit_count, status, precision, novelty, reason) VALUES (?,?,?,'pubmed',?,?,?,?,?,?)",
                     (topic_id, r["query"], r["origin"], run, r["count"], r["status"], r["precision"], r.get("novelty"), r["reason"]))
        n += 1
    conn.commit()
    return n


def design(conn, topic_id, cands=None, judge_fn=None, origin=None):
    # 候補を評価して決定し、保存する。cands が無ければトピックの定義から作る。判定の関数は差し替えられる(テスト用)。表(辞書のリスト)を返す
    st = settings()
    if cands is None:
        defn = json.loads(conn.execute("SELECT description FROM topics WHERE id=?", (topic_id,)).fetchone()["description"])
        cands = candidates(defn)
    if origin:
        cands = [{**c, "origin": origin} for c in cands]
    judge = judge_fn or default_judge(conn, topic_id)
    run = stages.new_run(conn, "query_design", load_config().get("screening_model"), None, {**st, "n_candidates": len(cands)})
    rows = [evaluate(c, judge, st) for c in cands[:st["max_candidates"]]]
    decide(rows, union_of_kept(conn, topic_id), st)
    register(conn, topic_id, rows, run)
    return [{k: r[k] for k in ("query", "origin", "why", "count", "sampled", "included", "precision", "novelty", "status", "reason")} for r in rows]


def gate(conn, topic_id, terms, origin="snowball", judge_fn=None):
    # 以後に加える式(知識駆動ループ・質問による追加収集)を、設計と同じ関門に通す。採用された式のリストを返す
    existing = {r[0].lower() for r in conn.execute("SELECT term FROM queries WHERE topic_id=?", (topic_id,))}
    cands = [{"query": t, "origin": origin, "why": "added later from accumulated knowledge or a question"} for t in dict.fromkeys(terms) if t.lower() not in existing]
    if not cands:
        return []
    table = design(conn, topic_id, cands, judge_fn, origin)
    return [r["query"] for r in table if r["status"] == "kept"]


# ---------- 5. 確認(人による上書き) ----------

def override(conn, topic_id, keep=(), drop=()):
    # 人の判断で式を採用/不採用にする(reviewer の代わりに reason に残す)。件数を返す
    n = 0
    for terms, status in ((keep, "kept"), (drop, "dropped")):
        for t in terms:
            cur = conn.execute("UPDATE queries SET status=?, reason=? WHERE topic_id=? AND lower(term)=lower(?)",
                               (status, f"manual override ({'keep' if status == 'kept' else 'drop'})", topic_id, t))
            n += cur.rowcount
    conn.commit()
    return n


def table(conn, topic_id):
    # このトピックの検索語と決定の一覧
    return [dict(r) for r in conn.execute(
        "SELECT term AS query, origin, hit_count AS count, status, precision, novelty, reason FROM queries WHERE topic_id=? ORDER BY id", (topic_id,))]


# ---------- トピックの作成 ----------

def new_topic(conn, defn, criteria_text=None, profile_file=None):
    # 定義からトピックを作る: 定義を保存し、プロファイルと採用基準をLLMで下書きする(以後の試験判定に基準が必要)。自分の基準・プロファイルも指定できる
    row = conn.execute("SELECT id FROM topics WHERE name=?", (defn["topic_name"],)).fetchone()
    tid = row["id"] if row else stages.create_topic(conn, defn["topic_name"], [])
    conn.execute("UPDATE topics SET description=? WHERE id=?", (json.dumps(defn, ensure_ascii=False), tid))
    conn.commit()
    core = f"{orx(defn['subject_terms'])}" + (f" AND {orx(defn['method_terms'])}" if defn["method_terms"] else "")
    stages.setup_topic(conn, defn["topic_name"], [core], criteria_text, profile_file)
    return tid
