"""M1: ステージA(キーワード生成)とB(収集)、および recall 計算。"""
import csv
import json
import re
from pathlib import Path

import httpx

from app import profile as prof
from app import pubmed
from app.config import load_config


def new_run(conn, stage, model=None, model_version=None, params=None, notes=None):
    # runs テーブルに実行記録を1行追加し、その id を返す(CLAUDE.md ルール6)
    cfg = load_config()
    cur = conn.execute(
        "INSERT INTO runs(stage, model, model_version, prompt_version, params_json, notes) VALUES (?,?,?,?,?,?)",
        (stage, model, model_version, cfg["prompt_version"], json.dumps(params or {}), notes))
    conn.commit()
    return cur.lastrowid


def create_topic(conn, name, seeds, criteria=None):
    # トピックを作り、シード検索語(origin=seed)を登録する
    cur = conn.execute("INSERT INTO topics(name, criteria) VALUES (?,?)", (name, criteria))
    tid = cur.lastrowid
    run = new_run(conn, "create_topic", params={"seeds": seeds})
    for s in seeds:
        conn.execute("INSERT INTO queries(topic_id, term, origin, source, run_id) VALUES (?,?,?,?,?)",
                     (tid, s, "seed", "pubmed", run))
    conn.commit()
    return tid


def setup_topic(conn, name, seeds, criteria_text=None, profile_file=None):
    # トピックを作る(同名があれば再利用)。プロファイルと基準が未設定なら、LLMで下書きして保存する(人が後から編集できる)
    row = conn.execute("SELECT id FROM topics WHERE name=?", (name,)).fetchone()
    tid = row["id"] if row else create_topic(conn, name, seeds)
    t = conn.execute("SELECT criteria, profile FROM topics WHERE id=?", (tid,)).fetchone()
    if profile_file:
        p = json.load(open(profile_file))
        errs = prof.validate(p)
        if errs:
            raise SystemExit("profile file has problems:\n- " + "\n- ".join(errs))
    elif not t["profile"]:
        print("generating topic profile with the LLM ...", flush=True)
        p, ok = prof.generate(name, seeds)
        print("profile generated" if ok else "profile: using generic fallback (edit profiles/*.json)", flush=True)
        p["schema_version"] = f"t{tid}-1"
    else:
        p = None
    if p:
        print("profile saved:", prof.save(conn, tid, p))
    if criteria_text:
        conn.execute("UPDATE topics SET criteria=? WHERE id=?", (criteria_text, tid))
    elif not t["criteria"]:
        print("drafting screening criteria with the LLM ...", flush=True)
        conn.execute("UPDATE topics SET criteria=? WHERE id=?", (prof.generate_criteria(name, seeds), tid))
    conn.commit()
    return tid


def _llm_terms(topic_name, seeds, hint):
    # ローカルLLM(Ollama)に PubMed 検索式の候補を出させる。温度0・seed固定でぶれを抑える
    cfg = load_config()
    prompt = (
        f"Research topic: {topic_name}\nSeed queries: {seeds}\n"
        "Propose 15 additional PubMed search queries (boolean strings using AND/OR) covering synonyms, abbreviations, "
        f"{hint}. Each query must stay on topic. "
        'Return JSON only: {"terms": ["...", ...]}'
    )
    url = cfg["ollama_url"]
    r = httpx.post(f"{url}/api/generate", timeout=600, json={
        "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False,
        "format": "json", "options": {"temperature": 0, "seed": 0}})
    r.raise_for_status()
    terms = json.loads(r.json()["response"]).get("terms", [])
    # モデルのダイジェスト(版の識別子)を取得して記録に残す
    tags = httpx.get(f"{url}/api/tags", timeout=30).json()["models"]
    digest = next((m["digest"][:12] for m in tags if m["name"] == cfg["models"]["llm"]), None)
    return [t for t in terms if isinstance(t, str) and t.strip()], digest


def generate_keywords(conn, topic_id):
    # LLM案とMeSH語を作り、PubMedで件数>0のものだけ queries に残す(ステージA)
    cfg = load_config()
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
    seeds = [r["term"] for r in conn.execute(
        "SELECT term FROM queries WHERE topic_id=? AND origin='seed'", (topic_id,))]
    llm_terms, digest = _llm_terms(topic["name"], seeds, prof.load(conn, topic_id)["keyword_hint"])
    run = new_run(conn, "keywords", cfg["models"]["llm"], digest, {"n_llm": len(llm_terms)})
    end = cfg["search_end_date"]

    # シード語がPubMedでどのMeSH語に対応づけられたかを、検索式の翻訳結果から取り出す
    mesh_terms = []
    for s in seeds:
        tr = pubmed.search(s, end)["translation"]
        for m in re.findall(r'"([^"]+)"\[MeSH Terms\]', tr):
            if m not in mesh_terms:
                mesh_terms.append(m)

    existing = {r["term"].lower() for r in conn.execute("SELECT term FROM queries WHERE topic_id=?", (topic_id,))}
    kept = dropped = 0
    for term, origin in [(t, "llm") for t in llm_terms] + [(m, "mesh") for m in mesh_terms]:
        if term.lower() in existing:
            continue
        existing.add(term.lower())
        count = pubmed.search(term, end)["count"]
        if count > 0:
            conn.execute("INSERT INTO queries(topic_id, term, origin, source, run_id, hit_count) VALUES (?,?,?,?,?,?)",
                         (topic_id, term, origin, "pubmed", run, count))
            kept += 1
        else:
            dropped += 1
    # シード語にも件数を記録する
    for s in seeds:
        conn.execute("UPDATE queries SET hit_count=? WHERE topic_id=? AND term=? AND origin='seed'",
                     (pubmed.search(s, end)["count"], topic_id, s))
    conn.commit()
    return {"kept": kept, "dropped_zero_hits": dropped, "mesh": mesh_terms}


def collect(conn, topic_id):
    # 全検索語をPubMedで実行し、PMIDを集めて論文情報を papers に保存する(ステージB)
    cfg = load_config()
    end, cap = cfg["search_end_date"], cfg["max_hits_per_query"]
    run = new_run(conn, "collect", params={"end_date": end, "cap": cap})
    qs = conn.execute("SELECT id, term FROM queries WHERE topic_id=? AND hit_count>0 AND (status IS NULL OR status='kept') ORDER BY id", (topic_id,)).fetchall()
    pmids, skipped = [], []
    for q in qs:
        res = pubmed.search(q["term"], end)
        if res["count"] > cap:
            # 広すぎる検索語は取り込まず、記録だけ残す(トピックから外れたノイズを避ける)
            skipped.append((q["term"], res["count"]))
            continue
        ids = pubmed.search(q["term"], end, retmax=res["count"])["ids"] if res["count"] else []
        pmids += [i for i in ids if i not in pmids]
    # 既に保存済みのPMIDは取得しない
    # このトピックが見つけた論文を紐づける(他のトピックで取得済みの論文も含む)
    for pm in pmids:
        conn.execute("INSERT OR IGNORE INTO topic_papers(topic_id, paper_id) SELECT ?, id FROM papers WHERE pmid=?", (topic_id, pm))
    have = {r[0] for r in conn.execute("SELECT pmid FROM papers")}
    new = [p for p in pmids if p not in have]
    added = 0
    for p in pubmed.fetch(new):
        conn.execute(
            "INSERT INTO papers(pmid, doi, pmcid, title, authors, year, journal, abstract, source, url, is_preprint, is_retracted, status)"
            " VALUES (:pmid,:doi,:pmcid,:title,:authors,:year,:journal,:abstract,'pubmed',:url,0,:is_retracted,'candidate')", p)
        conn.execute("INSERT OR IGNORE INTO topic_papers(topic_id, paper_id) SELECT ?, id FROM papers WHERE pmid=?", (topic_id, p["pmid"]))
        added += 1
    conn.execute("UPDATE runs SET notes=? WHERE id=?",
                 (json.dumps({"skipped_too_broad": skipped, "total_ids": len(pmids), "added": added}), run))
    conn.commit()
    return {"total_ids": len(pmids), "added": added, "skipped_too_broad": skipped}


def recall(conn, topic_name, topic_id=None):
    # tests/golden/<topic>.csv(列 pmid)と収集結果を突き合わせて recall を出す。無ければ「未測定」
    path = Path("tests/golden") / f"{topic_name}.csv"
    if not path.exists():
        return {"recall": None, "note": f"golden set missing ({path}): recall unmeasured"}
    gold = [r["pmid"].strip() for r in csv.DictReader(path.open()) if r.get("pmid")]
    have = {r[0] for r in conn.execute(
        "SELECT p.pmid FROM papers p WHERE ? IS NULL OR p.id IN (SELECT paper_id FROM topic_papers WHERE topic_id=?)", (topic_id, topic_id))}
    found = [g for g in gold if g in have]
    return {"recall": len(found) / len(gold), "found": len(found), "total": len(gold),
            "missing": [g for g in gold if g not in have]}


def _norm_title(t):
    # タイトルを小文字+英数字のみにして、表記ゆれ(記号・大文字小文字)を無視できるようにする
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def dedup(conn):
    # ステージC: DOI → PMID → (正規化タイトル+筆頭著者+年) の順で同一論文を探し、後から来た方を重複として正本に紐づける
    papers = conn.execute(
        "SELECT id, doi, pmid, title, authors, year FROM papers WHERE primary_paper_id IS NULL ORDER BY id").fetchall()
    seen_doi, seen_pmid, seen_key = {}, {}, {}
    merged = []
    for p in papers:
        first = (p["authors"] or "").split(";")[0].split(" ")[0].lower()
        key = (_norm_title(p["title"]), first, p["year"]) if p["title"] else None
        primary = (seen_doi.get(p["doi"]) if p["doi"] else None) \
            or (seen_pmid.get(p["pmid"]) if p["pmid"] else None) \
            or (seen_key.get(key) if key else None)
        if primary:
            conn.execute("UPDATE papers SET primary_paper_id=?, status='duplicate' WHERE id=?", (primary, p["id"]))
            merged.append((p["id"], primary))
            continue
        if p["doi"]: seen_doi[p["doi"]] = p["id"]
        if p["pmid"]: seen_pmid[p["pmid"]] = p["id"]
        if key: seen_key[key] = p["id"]
    new_run(conn, "dedup", notes=json.dumps({"merged": len(merged)}))
    conn.commit()
    return {"checked": len(papers), "merged": len(merged), "pairs": merged[:20]}


def _screen_one(cfg, criteria, p):
    # 1本をLLMで判定する。形式が壊れていたら1回だけ再試行し、それでもダメなら unsure にする
    abstract = (p["abstract"] or "")[:3000] or "(no abstract available)"
    prompt = (
        "You screen papers for a literature review. Apply these criteria strictly.\n\n"
        f"CRITERIA:\n{criteria}\n\nPAPER:\nTitle: {p['title']}\nYear: {p['year']}\nAbstract: {abstract}\n\n"
        'Answer with JSON only: {"label": "include" | "exclude" | "unsure", "reason": "<one short sentence>"}'
    )
    for _ in range(2):
        r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=600, json={
            "model": cfg["models"]["llm"], "prompt": prompt, "stream": False, "think": False,
            "format": "json", "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 150}})
        r.raise_for_status()
        try:
            out = json.loads(r.json()["response"])
            if out.get("label") in ("include", "exclude", "unsure") and out.get("reason"):
                return out["label"], str(out["reason"])[:300]
        except json.JSONDecodeError:
            pass
    return "unsure", "invalid LLM output after retry"


# 判定に使う語: 解析対象(ニコチン関連)が題名・要旨に全く無ければ、LLMを使わず除外できる
ANALYTE = re.compile(r"nicotin|cotinin|anabasin|anatabin", re.I)


# 題名に手法を示す語がある論文を先に判定する(予算内で関連の高いものから処理するため)
METHOD_FIRST = re.compile(r"determination|quantif|validat|method|assay|simultaneous|detection|analysis of|measurement|development", re.I)


def screen(conn, topic_id, limit=None, model=None, workers=1, boost=None):
    # ステージD: 未判定の正本論文を分類する。ルール(解析対象の語が無い→除外)→LLM の順。途中で止めても再開できる。
    # limit はLLMで判定する件数の上限(ルール除外は無料なので数えない)。boost(語のリスト)があれば、それを多く含む論文を先にLLMで判定する
    from concurrent.futures import ThreadPoolExecutor
    cfg = load_config()
    # 使うモデル: 引数 > config の screening_model > models.llm の順
    model = model or cfg.get("screening_model")
    if model:
        cfg["models"]["llm"] = model
    criteria = conn.execute("SELECT criteria FROM topics WHERE id=?", (topic_id,)).fetchone()["criteria"]
    P = prof.load(conn, topic_id)
    ent, first = prof.rx(P, "entity_regex"), prof.rx(P, "title_priority_regex")
    if not criteria:
        raise SystemExit("topic has no criteria: run set-criteria first")
    tags = httpx.get(f"{cfg['ollama_url']}/api/tags", timeout=30).json()["models"]
    digest = next((m["digest"][:12] for m in tags if m["name"] == cfg["models"]["llm"]), None)
    run = new_run(conn, "screen", cfg["models"]["llm"], digest, {"limit": limit, "workers": workers})
    todo = conn.execute(
        "SELECT p.* FROM papers p JOIN topic_papers tp ON tp.paper_id=p.id AND tp.topic_id=? "
        "WHERE p.primary_paper_id IS NULL AND p.is_retracted=0 AND NOT EXISTS "
        "(SELECT 1 FROM screening s WHERE s.paper_id=p.id AND s.topic_id=?) ORDER BY p.id", (topic_id, topic_id)).fetchall()
    ins = "INSERT INTO screening(paper_id, topic_id, label, reason, reviewer) VALUES (?,?,?,?,?)"
    # 要旨があるのに解析対象の語が無い論文は、ルールで除外(理由を残す)
    need_llm = []
    for p in todo:  # ルール判定は全件に行う(LLMを使わないので上限の対象外)
        text = (p["title"] or "") + " " + (p["abstract"] or "")
        if p["abstract"] and not ent.search(text):
            conn.execute(ins, (p["id"], topic_id, "exclude", f"rule: none of the topic terms ({P['entity_regex'][:60]}) in title or abstract", "rule"))
        else:
            need_llm.append(p)
    conn.commit()
    # LLMで判定する順: 質問の語を多く含む論文 → 題名に手法語がある論文 → 古いID。上限までを判定する
    def score(p):
        text = ((p["title"] or "") + " " + (p["abstract"] or "")).lower()
        return -sum(w in text for w in (boost or []))
    need_llm = sorted(need_llm, key=lambda p: (score(p), 0 if first.search(p["title"] or "") else 1, p["id"]))[:limit]
    # 残りをLLMで判定する(通信は並列、DB書き込みはこのスレッドだけで行う)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (p, (label, reason)) in enumerate(zip(need_llm, ex.map(lambda p: _screen_one(cfg, criteria, p), need_llm)), 1):
            conn.execute(ins, (p["id"], topic_id, label, reason, "llm"))
            conn.commit()
            if i % 50 == 0:
                print(f"screened {i}/{len(need_llm)}", flush=True)
    conn.execute("UPDATE runs SET notes=? WHERE id=?",
                 (json.dumps({"total": len(todo), "llm_calls": len(need_llm)}), run))
    conn.commit()
    return screen_summary(conn, topic_id)


def screen_summary(conn, topic_id):
    # ラベル別の件数を返す(PRISMA風の集計用)
    rows = conn.execute("SELECT label, reviewer, COUNT(*) n FROM screening WHERE topic_id=? GROUP BY label, reviewer",
                        (topic_id,)).fetchall()
    return {f"{r['label']}/{r['reviewer']}": r["n"] for r in rows}
