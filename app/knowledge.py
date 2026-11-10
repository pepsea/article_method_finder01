"""知識の蓄積と利用: 構造化抽出 → 分類 → 統合(ランキング) → 報告 → 目標チェック → 知識に基づく追加検索。"""
import json
import re
from collections import Counter, defaultdict

import httpx

from app import fulltext as ft
from app import profile as prof
from app import pubmed, stages
from app.config import load_config

SCHEMA_VERSION = "1.1"  # 旧名(ニコチン・トピック1の既定)。実際の版はプロファイルの schema_version を使う
LABEL_FIELDS = {"validation_type"}  # 原文に無い分類ラベル。引用なしで保持し inferred と記録する
LABEL_VALUES = {"none", "internal", "independent", "experimental"}
ROLES = prof.ROLES
# 旧名(テストやノートブックの互換用): ニコチンの組み込みプロファイルの値
FIELDS = {k: v["desc"] for k, v in prof.NICOTINE["roles"].items()}
ANALYTE = re.compile(prof.NICOTINE["entity_regex"], re.I)
METHOD_TITLE = re.compile(prof.GENERIC_TITLE, re.I)


def norm(s):
    # 空白の違いを無視して比べるための正規化(SPEC §12)
    return re.sub(r"\s+", " ", s or "").strip()


def loose(s):
    # 照合用: 大文字小文字・空白・ダッシュ種類・µ/μ の違いを無視する(原文に実在するかだけを見る)
    t = re.sub(r"[\s\u2009\u00a0]+", "", (s or "").lower())
    return t.replace("–", "-").replace("−", "-").replace("‐", "-").replace("μ", "µ")


def contains(quote, text):
    # 引用が本文に実在するか
    q = loose(quote)
    return bool(q) and q in loose(text)


# ---------- 抽出 ----------

def build_sources(conn, paper):
    # 抽出に使う本文を (セクション名, 本文) のリストにする。ライセンスOKの全文があれば Methods/Results も使う
    src = [("Title", paper["title"] or ""), ("Abstract", paper["abstract"] or "")]
    for name, cap in ft.READ_CAPS.items():
        t = " ".join(r["text"] for r in conn.execute(
            "SELECT text FROM sections WHERE paper_id=? AND name=?", (paper["id"], name)))
        if t:
            src.append((name, t[:cap]))
    return [(n, t) for n, t in src if t]


def verify_fields(raw, sources, P=None):
    # 各項目の引用が本文に実在するか確認する。実在しない項目は null にして、却下した値を残す(S2)
    P = P or prof.NICOTINE
    out, rejected = {}, 0
    for f in ROLES:
        # LLMには役割ごとの表示名で答えさせているので、表示名→保存キー(役割名)に戻す
        got = raw.get(P["roles"][f]["name"]) or raw.get(f)
        item = got if isinstance(got, dict) else {}
        v, q = item.get("value"), item.get("quote")
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        if v is not None and str(v).strip().lower() in ("", "null", "not stated", "not reported", "n/a", "none specified"):
            v = None
        loc = next((n for n, t in sources if q and contains(str(q), t)), None) if v is not None else None
        if f in LABEL_FIELDS:
            # 分類ラベル: 許可された値のみ。引用が実在すれば verified、無ければ inferred
            ok = v is not None and str(v).strip().lower() in LABEL_VALUES
            out[f] = {"value": str(v).strip().lower() if ok else None, "quote": norm(str(q)) if loc else None,
                      "location": loc, "verified": bool(loc and ok), "inferred": bool(ok and not loc)}
        elif v is not None and loc:
            out[f] = {"value": v, "quote": norm(str(q)), "location": loc, "verified": True}
        else:
            rejected += v is not None
            out[f] = {"value": None, "quote": None, "location": None, "verified": False, "rejected_value": v}
    return out, rejected


def extract_one(cfg, conn, paper, P=None):
    # 1本をLLMで抽出して検証する。JSON崩れは1回だけ再試行
    sources = build_sources(conn, paper)
    text = "\n\n".join(f"[{n}]\n{t}" for n, t in sources)
    P = P or prof.NICOTINE
    spec = "\n".join(f'- "{P["roles"][k]["name"]}": {P["roles"][k]["desc"]}' for k in ROLES)
    prompt = (
        f"You extract structured data from a paper about {P['context']}. Use ONLY the text below.\n"
        'For every field return {"value": <short string>, "quote": <EXACT contiguous copy of at most 150 characters from the text that supports the value>}.\n'
        'If the text does not state the field, return {"value": null, "quote": null}. Never guess. Copy numbers and units exactly as written.\n\n'
        f"FIELDS:\n{spec}\n\nTEXT:\n{text}\n\nReturn one JSON object whose keys are the field names.")
    for _ in range(2):
        r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=900, json={
            "model": cfg["extraction_model"], "prompt": prompt, "stream": False, "think": False, "format": "json",
            "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 900}})
        r.raise_for_status()
        try:
            raw = json.loads(r.json()["response"])
            if isinstance(raw, dict):
                return verify_fields(raw, sources, P)
        except json.JSONDecodeError:
            pass
    return None, 0


def usable2(fields, P=None):
    # 改訂版(v2): 対象(entity)がプロファイルの語に合い、手法クラスの記述が引用つきで取れていれば知識として使う(他の項目は有れば使う)
    ent = prof.rx(P or prof.NICOTINE, "entity_regex")
    return bool(ent.search(str(fields["analytes"]["value"] or ""))) and fields["sample_preparation"]["value"] is not None


def usable(fields, P=None):
    # 抽出直後の状態判定(ok/insufficient): 対象がプロファイルの語に合い、手法の詳細(手法・詳細A・詳細B・性能)が2つ以上ある
    an = fields["analytes"]["value"] or ""
    detail = sum(fields[k]["value"] is not None for k in ("sample_preparation", "chromatography_column", "internal_standard", "lloq"))
    return bool(prof.rx(P or prof.NICOTINE, "entity_regex").search(str(an))) and detail >= 2


def priority(p, title_rx=None):
    # 抽出の優先順: 手法を主題とする題名 > 全文あり。上位から処理する
    return (0 if (title_rx or METHOD_TITLE).search(p["title"] or "") else 1, 0 if p["licence"] and "creativecommons" in (p["licence"] or "") else 1, p["id"])


def extract_all(conn, topic_id, limit=None):
    # 採用論文のうち未抽出のものを、優先順に抽出する(再開可能)。抽出モデルは config の extraction_model
    cfg = load_config()
    tags = httpx.get(f"{cfg['ollama_url']}/api/tags", timeout=30).json()["models"]
    digest = next((m["digest"][:12] for m in tags if m["name"] == cfg["extraction_model"]), None)
    P = prof.load(conn, topic_id)
    sv, title_rx = P["schema_version"], prof.rx(P, "title_priority_regex")
    run = stages.new_run(conn, "extract", cfg["extraction_model"], digest, {"schema": sv, "limit": limit})
    todo = conn.execute(
        "SELECT p.* FROM papers p JOIN screening s ON s.paper_id=p.id AND s.topic_id=? WHERE s.label='include' "
        "AND NOT EXISTS (SELECT 1 FROM extractions e WHERE e.paper_id=p.id AND e.schema_version=?)",
        (topic_id, sv)).fetchall()
    # 抽出した後に全文が手に入った論文は、全文を使って抽出し直す(古い抽出は置き換える)
    refresh = conn.execute(
        "SELECT p.* FROM papers p JOIN screening s ON s.paper_id=p.id AND s.topic_id=? "
        "JOIN extractions e ON e.paper_id=p.id AND e.schema_version=? JOIN runs r ON r.id=e.run_id "
        "JOIN fulltext f ON f.paper_id=p.id AND f.status='ok' WHERE s.label='include' AND f.updated_at > r.started_at",
        (topic_id, sv)).fetchall()
    refresh_ids = {r["id"] for r in refresh}
    todo = sorted(todo + [r for r in refresh], key=lambda p: priority(p, title_rx))[:limit]
    stats = Counter()
    for i, p in enumerate(todo, 1):
        fields, rejected = extract_one(cfg, conn, p, P)
        if p["id"] in refresh_ids:
            conn.execute("DELETE FROM extractions WHERE paper_id=? AND schema_version=?", (p["id"], sv))
            stats["re_extracted"] += 1
        if fields is None:
            status, blob = "failed", "{}"
        else:
            status = "ok" if usable(fields, P) else "insufficient"
            blob = json.dumps({"fields": fields, "rejected": rejected}, ensure_ascii=False)
        conn.execute("INSERT INTO extractions(paper_id, schema_version, json, status, run_id) VALUES (?,?,?,?,?)",
                     (p["id"], sv, blob, status, run))
        conn.commit()
        stats[status] += 1
        if i % 25 == 0:
            print(f"extracted {i}/{len(todo)} {dict(stats)}", flush=True)
    return dict(stats)


# ---------- 分類(ルールベース。決定的で再実行しても変わらない) ----------

MATRIX_RULES = prof.NICOTINE["condition_classes"]
PREP_RULES = prof.NICOTINE["method_classes"]
COLUMN_RULES = prof.NICOTINE["detail_a_classes"]


def classify(text, rules, default="other"):
    # 最初に当てはまるルールの分類名を返す。null は None
    if not text:
        return None
    for name, pat in rules:
        if re.search(pat, str(text), re.I):
            return name
    return default


def matrix_classes(text, rules=None):
    # 1論文が複数の条件(試料など)を扱うことがあるので、当てはまる全てを返す(無ければ other)
    if not text:
        return []
    hits = [n for n, pat in (rules if rules is not None else MATRIX_RULES) if re.search(pat, str(text), re.I)]
    return hits or ["other"]


def parse_lloq(text):
    # "0.5 ng/mL" や "95 %" から (数値, 単位) を取り出す。変換や平均はしない(SPEC §7)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:±\s*[\d.]+\s*)?([a-zA-Zµμ]+\s*/\s*[a-zA-Z0-9]+|%)", str(text or ""))
    return (float(m.group(1).replace(",", ".")), re.sub(r"\s+", "", m.group(2)).replace("μ", "µ").lower()) if m else None


# ---------- 統合 ----------

def load_knowledge(conn, topic_id):
    # 使える抽出(status=ok)を、論文情報とつなげて返す
    P = prof.load(conn, topic_id)
    out = []
    for r in conn.execute(
            "SELECT p.id pid, p.pmid, p.doi, p.year, p.title, p.authors, p.is_preprint, e.json FROM extractions e "
            "JOIN papers p ON p.id=e.paper_id JOIN screening s ON s.paper_id=p.id AND s.topic_id=? "
            "WHERE e.status IN ('ok','insufficient') AND s.label='include' AND e.schema_version=?", (topic_id, P["schema_version"])):
        f = json.loads(r["json"])["fields"]
        if not usable2(f, P):
            continue
        last = (r["authors"] or "").split(";")[-1].strip().split(" ")[0].lower()  # 独立グループの代理: 最終著者の姓
        out.append({"pmid": r["pmid"], "doi": r["doi"], "year": r["year"], "title": r["title"], "group": last,
                    "preprint": r["is_preprint"], "f": f, "matrices": matrix_classes(f["matrix"]["value"], P["condition_classes"]),
                    "prep": classify(f["sample_preparation"]["value"], P["method_classes"]),
                    "column": classify(f["chromatography_column"]["value"], P["detail_a_classes"]),
                    "lloq": parse_lloq(f["lloq"]["value"]), "validation": (f["validation_type"]["value"] or "").lower()})
    return out


def grade(items):
    # 証拠グレード(改訂v2、SPEC §8 の具体化): 数値つき検証(LLOQを報告)を出した「独立グループ」の数で決める
    #  A: 3グループ以上 / B: 2グループ / C: 1グループ / D: 数値つき検証なし、または全てプレプリント
    # LLMの validation_type ラベルは参考情報とし、判定には使わない(「実験的」を乱用するため)
    if all(i["preprint"] for i in items):
        return "D"
    groups = {i["group"] or i["pmid"] for i in items if i["lloq"]}
    return "D" if not groups else "A" if len(groups) >= 3 else "B" if len(groups) == 2 else "C"


def synthesize(conn, topic_id, min_papers=2):
    # マトリクス(行=前処理クラス、列=試料)を作り、試料ごとに最良の手法クラスを選ぶ
    P = prof.load(conn, topic_id)
    items = load_knowledge(conn, topic_id)
    cells = defaultdict(list)
    for it in items:
        for m in it["matrices"]:
            if it["prep"]:
                cells[(m, it["prep"])].append(it)
    recs = {}
    for m in sorted({k[0] for k in cells}):
        cands = []
        for (mm, prep), its in cells.items():
            if mm != m:
                continue
            g = grade(its)
            cands.append({"prep": prep, "n": len(its), "grade": g, "items": its})
        cands.sort(key=lambda c: (c["prep"] == "other", "ABCD".index(c["grade"]), -c["n"]))  # 「other」は最後
        best = cands[0]
        pool = best["items"]
        cols = [i["column"] for i in pool if i["column"]]
        col = Counter([c for c in cols if c not in P["detail_a_generic"]] or cols).most_common(1)
        # 内標は、化合物名を含む具体的な記載(例 nicotine-d4)を優先し、無ければ一般的な記載を使う
        specific = re.compile(P["detail_b_specific_regex"], re.I)
        vals = [(i["f"]["internal_standard"]["value"] or "").lower() for i in pool if i["f"]["internal_standard"]["value"]]
        isd = Counter([v for v in vals if specific.search(v)] or vals).most_common(1)
        by_unit = defaultdict(list)
        for i in pool:
            if i["lloq"]:
                by_unit[i["lloq"][1]].append((i["lloq"][0], i["pmid"]))
        conflicts = []
        for u, vals in by_unit.items():
            if len(vals) >= 2 and min(vals)[0] > 0 and max(vals)[0] / min(vals)[0] >= 10:
                conflicts.append({"unit": u, "low": min(vals), "high": max(vals), "ratio": round(max(vals)[0] / min(vals)[0])})
        recs[m] = {"conflicts": conflicts, "best": best["prep"], "grade": best["grade"], "n": best["n"], "column": col[0][0] if col else None,
                   "internal_standard": isd[0][0] if isd else None, "lloq": {u: (min(v) if P["performance_lower_is_better"] else max(v)) for u, v in by_unit.items()},
                   "alternatives": [(c["prep"], c["n"], c["grade"]) for c in cands[1:]], "papers": pool,
                   "n_matrix_total": len({i["pmid"] for c in cands for i in c["items"]})}
    return {"n_usable": len(items), "cells": {k: len(v) for k, v in cells.items()}, "recs": recs}


# ---------- 報告 ----------

def reverify(conn, topic_id):
    # 報告に使う全引用を、保存済み本文に対してもう一度照合する(S2)。(照合数, 失敗リスト)を返す
    total, bad = 0, []
    sv = prof.load(conn, topic_id)["schema_version"]
    for r in conn.execute(
            "SELECT p.*, e.json FROM extractions e JOIN papers p ON p.id=e.paper_id JOIN topic_papers tp ON tp.paper_id=p.id AND tp.topic_id=? "
            "WHERE e.status='ok' AND e.schema_version=?", (topic_id, sv)):
        sources = build_sources(conn, r)
        for k, v in json.loads(r["json"])["fields"].items():
            if v["value"] is not None and not v.get("inferred"):
                total += 1
                if not any(contains(v["quote"], t) for _, t in sources):
                    bad.append((r["pmid"], k))
    return total, bad


def references(conn, pmids):
    # 報告で引用したPMIDの文献リスト(著者・年・題名・雑誌・DOI)。DBにある情報だけを使う
    out = []
    for pm in sorted(set(pmids)):
        r = conn.execute("SELECT pmid, authors, year, title, journal, doi FROM papers WHERE pmid=?", (str(pm),)).fetchone()
        if r:
            au = (r["authors"] or "").split(";")
            out.append({"pmid": r["pmid"], "authors": au[0].strip() + (" et al." if len(au) > 1 else ""), "year": r["year"],
                        "title": r["title"], "journal": r["journal"], "doi": r["doi"]})
    return out


def review_sheet(conn, topic_id, folder, frac=0.07, seed=0):
    # 品質管理(S3): 抽出結果の5〜10%の論文を無作為に選び、人が正誤を記入するCSVを書く(correct 列に y / n)
    import csv, random
    P = prof.load(conn, topic_id)
    rows = conn.execute(
        "SELECT p.pmid, e.json FROM extractions e JOIN papers p ON p.id=e.paper_id JOIN screening s ON s.paper_id=p.id AND s.topic_id=? "
        "WHERE s.label='include' AND e.schema_version=? AND e.status IN ('ok','insufficient') ORDER BY p.pmid", (topic_id, P["schema_version"])).fetchall()
    pick = random.Random(seed).sample(rows, min(len(rows), max(5, round(len(rows) * frac)))) if rows else []
    path = folder / "review_sheet.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pmid", "field", "extracted_value", "quote", "location", "correct (y/n)", "note"])
        for r in pick:
            for role, v in json.loads(r["json"])["fields"].items():
                if v["value"] is not None:
                    w.writerow([r["pmid"], P["roles"][role]["label"], v["value"], v.get("quote") or "(inferred label)", v.get("location") or "", "", ""])
    return {"path": str(path), "papers": len(pick), "of": len(rows)}


def score_review_sheet(path):
    # 人が記入したレビューシートから、抽出の正解率(S3: 目標 85%以上)を計算する
    import csv
    ys = ns = 0
    for r in csv.DictReader(open(path)):
        a = (r.get("correct (y/n)") or "").strip().lower()
        ys += a == "y"; ns += a == "n"
    done = ys + ns
    return {"checked": done, "correct": ys, "accuracy": round(ys / done, 3) if done else None, "target_met": bool(done and ys / done >= 0.85)}


def labels(P):
    # 報告に使う表示名(試料タイプ、手法、詳細A/B、性能)をプロファイルから取る
    r = P["roles"]
    return {"cond": r["matrix"]["label"], "method": r["sample_preparation"]["label"], "a": r["chromatography_column"]["label"],
            "b": r["internal_standard"]["label"], "perf": r["lloq"]["label"],
            "best": "Lowest" if P["performance_lower_is_better"] else "Highest"}


def report_parts(conn, topic_id, syn, goal, run_label):
    # 報告の内容部品(HTML断片)を作る。内容はここだけで作り、デザイン(CSS/レイアウト)は既定または Gemma の雛形が担当する
    from html import escape as e
    from app import export
    P = prof.load(conn, topic_id)
    L = labels(P)
    c = export.counts(conn, topic_id)
    n_ext = conn.execute("SELECT COUNT(*) FROM extractions x JOIN topic_papers tp ON tp.paper_id=x.paper_id AND tp.topic_id=? "
                         "WHERE x.schema_version=? AND x.status!='failed'", (topic_id, P["schema_version"])).fetchone()[0]
    recs = [(m, r) for m, r in sorted(syn["recs"].items(), key=lambda kv: -kv[1]["n_matrix_total"]) if r["n_matrix_total"] >= 2]
    pm = lambda x: f"<a href='https://pubmed.ncbi.nlm.nih.gov/{e(str(x))}/' target='_blank'>PMID {e(str(x))}</a>"
    lq = lambda r: "; ".join(f"{v[0]:g} {e(u)} {pm(v[1])}" for u, v in r["lloq"].items()) or "not reported"
    rows = "".join(
        f"<tr><td><a href='#c{i}'>{e(m)}</a></td><td>{r['n_matrix_total']}</td><td><b>{e(r['best'])}</b> ({r['n']})</td>"
        f"<td>{e(r['column'] or 'n/a')}</td><td>{e((r['internal_standard'] or 'n/a')[:70])}</td><td>{lq(r)}</td>"
        f"<td><span class='g g{r['grade']}'>{r['grade']}</span></td></tr>" for i, (m, r) in enumerate(recs))
    glance = (f"<table><tr><th>{e(L['cond'])}</th><th>Papers</th><th>Best {e(L['method'].lower())} (papers)</th><th>Typical {e(L['a'].lower())}</th>"
              f"<th>Typical {e(L['b'].lower())}</th><th>{L['best']} reported {e(L['perf'])}</th><th>Evidence</th></tr>{rows}</table>")
    cards, cited = "", set()
    for i, (m, r) in enumerate(recs):
        alts = ", ".join(f"{e(a)} (n={n}, grade {g})" for a, n, g in r["alternatives"]) or "none"
        cites = ""
        for it in r["papers"][:4]:
            cited.add(it["pmid"])
            q = it["f"]["sample_preparation"]
            cites += f"<li>{pm(it['pmid'])} ({it['year']}): <q>{e(q['quote'] or '')}</q> <span class='muted'>[{e(q['location'] or '')}]</span></li>"
        cards += (f"<section class='card' id='c{i}'><h3>{e(m)} <span class='muted'>({r['n_matrix_total']} papers)</span></h3>"
                  f"<p><b>Recommended {e(L['method'].lower())}:</b> {e(r['best'])} &mdash; {r['n']} papers, evidence grade <span class='g g{r['grade']}'>{r['grade']}</span></p>"
                  f"<p>Typical {e(L['a'].lower())}: {e(r['column'] or 'n/a')}<br>Typical {e(L['b'].lower())}: {e(r['internal_standard'] or 'n/a')}<br>"
                  f"{L['best']} reported {e(L['perf'])} by unit: {lq(r)}<br>Alternatives: {alts}</p><ul>{cites}</ul></section>")
    classes = [n for n, _ in P["method_classes"]] + ["other"]
    mats = sorted({k[0] for k in syn["cells"]})
    mx = max(syn["cells"].values() or [1])
    head = "".join(f"<th>{e(p)}</th>" for p in classes)
    body = "".join("<tr><th>" + e(m) + "</th>" + "".join(
        f"<td style='background:rgba(26,95,180,{0.08 + 0.55 * syn['cells'].get((m, p), 0) / mx:.2f})'>{syn['cells'].get((m, p), 0) or ''}</td>" for p in classes) + "</tr>" for m in mats)
    matrix = f"<div class='scroll'><table><tr><th>{e(L['cond'].lower())}</th>{head}</tr>{body}</table></div>"
    search = (f"<ul><li>Queries: {c['queries']}</li><li>Collected from PubMed: {c['collected']} (duplicates merged: {c['duplicates_merged']})</li>"
              f"<li>Screened: include {c['include']}, exclude {c['exclude']}, unsure {c['unsure']}, not screened {c['not_screened']}</li>"
              f"<li>Knowledge extracted: {n_ext} papers, of which {syn['n_usable']} usable</li></ul>")
    goals = "<ul>" + "".join(f"<li><span class='badge {'ok' if v['ok'] else 'ng'}'>{'PASS' if v['ok'] else 'FAIL'}</span> <b>{e(k)}</b>: {e(v['detail'])}</li>" for k, v in goal.items()) + "</ul>"
    conf = "".join(f"<li><b>{e(m)}</b> ({e(cf['unit'])}): {cf['low'][0]:g} ({pm(cf['low'][1])}) vs {cf['high'][0]:g} ({pm(cf['high'][1])}), {cf['ratio']}x apart</li>"
                   for m, r in recs for cf in r["conflicts"])
    conflicts = f"<ul>{conf}</ul>" if conf else "<p class='muted'>No reported values differ by 10x or more within a recommended method class.</p>"
    limits = ["Classes are assigned by keyword rules (the topic profile) on extracted text; review them in the taxonomy step.",
              f"Evidence grade counts independent groups that report a numeric {L['perf']}; the last-author surname is a proxy for a group.",
              "Extraction used abstracts, plus Methods/Results only for papers with a CC BY-type licence.",
              "Conflicts are flagged only when numeric values of the same unit differ 10-fold or more.", "Recall against a hand-made golden set is measured only when a golden set is supplied.",
              "\u201cBest\u201d means best-supported in the literature, not a head-to-head performance comparison."]
    lim = "<ul>" + "".join(f"<li>{e(x)}</li>" for x in limits) + "</ul>"
    refs = "<ol>" + "".join(f"<li>{e(r['authors'])} ({r['year']}). {e(r['title'] or '')} <i>{e(r['journal'] or '')}</i>. {pm(r['pmid'])}"
                            + (f", DOI {e(r['doi'])}" if r["doi"] else "") + "</li>" for r in references(conn, cited)) + "</ol>"
    tax = conn.execute("SELECT t.name, COUNT(g.paper_id) n FROM taxonomy t LEFT JOIN paper_tags g ON g.taxonomy_id=t.id WHERE t.topic_id=? GROUP BY t.id ORDER BY n DESC", (topic_id,)).fetchall()
    taxonomy = ("<table><tr><th>Class</th><th>Papers tagged</th></tr>" + "".join(f"<tr><td>{e(r['name'])}</td><td>{r['n']}</td></tr>" for r in tax) + "</table>") if tax else "<p class='muted'>Taxonomy not built yet.</p>"
    top = recs[:3]
    findings = "<ul>" + "".join(f"<li><b>{e(m)}:</b> {e(r['best'])} ({r['n']} papers, evidence {r['grade']})</li>" for m, r in top) + "</ul>" if top else "<p class='muted'>No recommendation yet.</p>"
    return {"cond_label": L["cond"], "method_label": L["method"], "glance": glance, "details": cards, "matrix": matrix, "search": search, "goal": goals,
            "conflicts": conflicts, "limits": lim, "references": refs, "taxonomy": taxonomy, "findings": findings,
            "n_usable": syn["n_usable"], "run": str(run_label)}


DEFAULT_CSS = """:root{--bg:#fafafa;--fg:#222;--card:#fff;--line:#ddd;--muted:#777;--link:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#e8e8e8;--card:#2a2a2d;--line:#444;--muted:#999;--link:#78aeed}}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg);margin:0;line-height:1.5}
main{max-width:1000px;margin:0 auto;padding:24px 16px} a{color:var(--link)} h1{font-size:24px} h2{font-size:18px;margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:4px} h3{font-size:16px;margin:0 0 6px}
table{border-collapse:collapse;width:100%;background:var(--card);font-size:13px} th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top} th{background:rgba(128,128,128,.12)}
section.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:12px 16px;margin:12px 0} .muted{color:var(--muted);font-weight:normal;font-size:13px} q{font-style:italic}
.g{display:inline-block;min-width:22px;text-align:center;border-radius:4px;padding:0 6px;font-weight:bold;color:#fff} .gA{background:#2e7d32} .gB{background:#558b2f} .gC{background:#b8860b} .gD{background:#9e9e9e}
.badge{border-radius:4px;padding:1px 6px;font-size:12px;font-weight:bold;color:#fff} .ok{background:#2e7d32} .ng{background:#c62828} ul{padding-left:20px} .scroll{overflow-x:auto}"""


def html_report(conn, topic_id, title, syn, goal, run_label):
    # 完全版レポート(既定デザイン): 一覧 → 詳細(引用) → 行列表 → 分類 → 矛盾 → 検索記録 → 目標チェック → 限界 → 参考文献
    from html import escape as e
    d = report_parts(conn, topic_id, syn, goal, run_label)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Best-method report</title><style>{DEFAULT_CSS}</style></head><body><main>
<h1>Best-method report: {e(title)}</h1>
<p class="muted">Run: {e(d['run'])} &middot; based on {d['n_usable']} papers with usable extracted knowledge &middot; every statement carries a PMID and a verbatim quote verified against the stored text</p>
<h2>Answer at a glance</h2><div class="scroll">{d['glance']}</div>
<h2>Details and supporting quotes</h2>{d['details']}
<h2>{e(d['method_label'])} &times; {e(d['cond_label'].lower())} (number of papers)</h2>{d['matrix']}
<h2>Taxonomy</h2>{d['taxonomy']}
<h2>Conflicting reports</h2>{d['conflicts']}
<h2>Search record</h2>{d['search']}
<h2>Goal check</h2>{d['goal']}
<h2>Limitations</h2>{d['limits']}
<h2>References</h2>{d['references']}
</main></body></html>"""


def write_report(conn, topic_id, folder, syn, goal, round_no):
    # Markdown と HTML のレポートを書く。各推奨に PMID と原文引用(検証済み)を付ける
    t = conn.execute("SELECT name FROM topics WHERE id=?", (topic_id,)).fetchone()["name"]
    P = prof.load(conn, topic_id)
    Lb = labels(P)
    L = [f"# Best-method report: {t}", f"Run: {round_no}. Based on {syn['n_usable']} papers with usable extracted knowledge.", ""]
    L.append(f"## Recommendation per {Lb['cond'].lower()}")
    for m, r in sorted(syn["recs"].items(), key=lambda kv: -kv[1]["n_matrix_total"]):
        if r["n_matrix_total"] < 2:
            continue
        L.append(f"### {m}  ({r['n_matrix_total']} papers)")
        lq = "; ".join(f"{v[0]} {u} (PMID {v[1]})" for u, v in r["lloq"].items()) or "not reported"
        L.append(f"- **{Lb['method']}:** {r['best']} ({r['n']} papers, evidence grade {r['grade']})")
        L.append(f"- Typical {Lb['a'].lower()}: {r['column'] or 'n/a'}; typical {Lb['b'].lower()}: {r['internal_standard'] or 'n/a'}")
        L.append(f"- {Lb['best']} reported {Lb['perf']} by unit: {lq}")
        L.append("- Alternatives: " + (", ".join(f"{a} (n={n}, grade {g})" for a, n, g in r["alternatives"]) or "none"))
        for i in r["papers"][:3]:
            q = i["f"]["sample_preparation"]
            L.append(f"  - PMID {i['pmid']} ({i['year']}): “{q['quote']}” [{q['location']}]")
        L.append("")
    names = [n for n, _ in P["method_classes"]] + ["other"]
    L += [f"## {Lb['method']} x {Lb['cond'].lower()} (number of papers)", "", f"| {Lb['cond'].lower()} | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for m in sorted({k[0] for k in syn["cells"]}):
        L.append(f"| {m} | " + " | ".join(str(syn["cells"].get((m, p), 0)) for p in names) + " |")
    L += ["", "## Conflicting reports"]
    cf = [f"- {m} ({c['unit']}): {c['low'][0]:g} (PMID {c['low'][1]}) vs {c['high'][0]:g} (PMID {c['high'][1]}), {c['ratio']}x apart"
          for m, r in syn["recs"].items() if r["n_matrix_total"] >= 2 for c in r["conflicts"]]
    L += cf or ["- none flagged"]
    L += ["", "## Goal check", ""] + [f"- {'PASS' if v['ok'] else 'FAIL'} **{k}**: {v['detail']}" for k, v in goal.items()]
    cited = {i["pmid"] for r in syn["recs"].values() if r["n_matrix_total"] >= 2 for i in r["papers"][:4]}
    L += ["", "## References"] + [f"- {x['authors']} ({x['year']}). {x['title']} {x['journal'] or ''}. PMID {x['pmid']}" for x in references(conn, cited)]
    L += ["", "## Limitations", "- Classes are assigned by keyword rules (the topic profile); the taxonomy has not yet been reviewed by the owner.",
          "- Evidence grade uses the last-author surname as a proxy for an independent group.",
          "- Extraction used abstracts, and Methods/Results only for papers with a CC BY-type licence.",
          "- Conflict detection between papers is not implemented yet.", "- Recall against a hand-made golden set is not measured."]
    (folder / "report.md").write_text("\n".join(L))
    (folder / "report.html").write_text(html_report(conn, topic_id, t, syn, goal, round_no))


# ---------- 目標チェック ----------

def check_goal(conn, topic_id, syn):
    # 目標: 主要な条件ごとに、引用つきで根拠のある最良手法を出せているか(チェック項目の表示名はプロファイルから)
    P = prof.load(conn, topic_id)
    Lb = labels(P)
    n_inc = conn.execute("SELECT COUNT(*) FROM screening WHERE topic_id=? AND label='include'", (topic_id,)).fetchone()[0]
    n_ext = conn.execute("SELECT COUNT(*) FROM extractions x JOIN topic_papers tp ON tp.paper_id=x.paper_id AND tp.topic_id=? "
                         "WHERE x.schema_version=? AND x.status!='failed'", (topic_id, P["schema_version"])).fetchone()[0]
    ok_rate = syn["n_usable"] / n_ext if n_ext else 0
    total, bad = reverify(conn, topic_id)
    strong = [m for m, r in syn["recs"].items() if r["n_matrix_total"] >= 3 and m != "other"]
    full = [m for m in strong if syn["recs"][m]["column"] and syn["recs"][m]["internal_standard"] and syn["recs"][m]["lloq"]]
    goal = {
        "G1 coverage": {"ok": len(strong) >= 5, "detail": f"{len(strong)} {Lb['cond'].lower()}s have >=3 usable papers ({', '.join(strong)})"},
        "G2 citations verified (S2)": {"ok": total > 0 and not bad, "detail": f"{total - len(bad)}/{total} quotes found in stored text"},
        "G3 recommendation completeness": {"ok": bool(strong) and len(full) == len(strong), "detail": f"{len(full)}/{len(strong)} recommendations have {Lb['a'].lower()}, {Lb['b'].lower()} and {Lb['perf']}"},
        "G4 usable extraction rate": {"ok": ok_rate >= 0.7, "detail": f"{syn['n_usable']}/{n_ext} extractions usable ({ok_rate:.0%}); included papers {n_inc}"},
    }
    sc = P.get("sanity")
    if sc:
        # トピック固有の常識チェック(例: 尿・血漿では同位体標識内標が推奨されているか)
        key = {"internal_standard": "internal_standard", "chromatography_column": "column"}.get(sc["field"], "internal_standard")
        present = [m for m in sc["conditions"] if m in syn["recs"]]
        hit = [m for m in present if re.search(sc["regex"], syn["recs"][m][key] or "", re.I)]
        goal["G5 sanity: " + sc["label"]] = {"ok": bool(hit) and len(hit) == len(present), "detail": f"satisfied for: {', '.join(hit) or 'none'} (of {', '.join(present) or 'none present'})"}
    goal["G6 evidence not all grade D"] = {"ok": any(r["grade"] in "ABC" for r in syn["recs"].values()), "detail": "grades: " + ", ".join(f"{m}={r['grade']}" for m, r in syn["recs"].items() if r["n_matrix_total"] >= 3)}
    return goal


# ---------- 知識に基づく追加検索 ----------

def add_query(conn, topic_id, term, run):
    # 新しい検索語を、件数があり未登録のときだけ追加する。追加できたら True
    if conn.execute("SELECT 1 FROM queries WHERE topic_id=? AND lower(term)=lower(?)", (topic_id, term)).fetchone():
        return False
    n = pubmed.search(term, load_config()["search_end_date"])["count"]
    if n == 0:
        return False
    conn.execute("INSERT INTO queries(topic_id, term, origin, source, run_id, hit_count) VALUES (?,?,?,'pubmed',?,?)",
                 (topic_id, term, "snowball", run, n))
    conn.commit()
    return True


def knowledge_queries(conn, topic_id, syn, min_papers=5):
    # 知識の穴から次の検索語を作る: ①論文が少ない条件(試料など) ②抽出で頻出するが未検索の対象(代謝物など)
    P = prof.load(conn, topic_id)
    run = stages.new_run(conn, "knowledge_queries", notes="queries derived from accumulated knowledge")
    new = []   # 候補の式。最後に、設計と同じ関門(試験サンプルの関連率・重複・合計の上限)を通して、採用された式だけを返す
    have = {m: r["n_matrix_total"] for m, r in syn["recs"].items()}
    for m, terms in P["gap_terms"].items():
        if have.get(m, 0) < min_papers:
            new.append(P["gap_query_template"].replace("{terms}", terms))
    ent = prof.rx(P, "entity_regex")
    counts = Counter()
    for it in load_knowledge(conn, topic_id):
        for a in re.split(r"[;,]| and ", (it["f"]["analytes"]["value"] or "").lower()):
            a = re.sub(r"\(.*?\)", "", a).strip()
            if 4 < len(a) < 40 and ent.search(a):
                counts[a] += 1
    known = " ".join(r["term"].lower() for r in conn.execute("SELECT term FROM queries WHERE topic_id=?", (topic_id,)))
    for a, c in counts.most_common(15):
        if c >= 2 and a not in known:
            new.append(P["entity_query_template"].replace("{entity}", a))
    from app import querydesign
    return querydesign.gate(conn, topic_id, new, "snowball")
