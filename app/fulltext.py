"""全文の取得(PMC と Europe PMC)。JATS XML をセクションに分け、ライセンスを記録する。商用利用できる自由ライセンスの本文だけ保存する。"""
import re
import xml.etree.ElementTree as ET
from collections import Counter

from app import europepmc, pubmed

XLINK = "{http://www.w3.org/1999/xlink}href"
READ_CAPS = {"Methods": 6000, "Results": 2500}  # 抽出で読むセクションと最大文字数(それ以外の節は抽出では読まない)


def licence_ok(href, text):
    # 商用利用できる自由ライセンスか判定する(CC BY / CC BY-SA / CC0 / パブリックドメインのみ可。NC・ND・不明は不可)
    s = ((href or "") + " " + (text or "")).lower()
    # 本文中の最初の Creative Commons URL で判定する(URLが無ければ不可)
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z0-9\-]+)", s)
    if m:
        return m.group(1) == "publicdomain" or m.group(2) in ("by", "by-sa")
    # URLが無く文章だけの場合: 「Creative Commons Attribution」と書かれ、非営利・改変禁止の語が無いときだけ CC BY として扱う
    return bool(re.search(r"creative commons attribution", s)) and not re.search(r"non-?commercial|no-?deriv|\bnc\b|\bnd\b", s)


def epmc_licence_url(s):
    # Europe PMC の license 欄("cc by", "cc by-nc", "cc0" など)を、判定できる Creative Commons のURL形式にする
    s = (s or "").strip().lower()
    if s.startswith("cc0") or "public domain" in s:
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    m = re.match(r"cc[ -]?([a-z\-]+)", s)
    return f"https://creativecommons.org/licenses/{m.group(1)}/" if m else None


def _section_name(title):
    # 見出しを Introduction/Methods/Results/Discussion/Other のいずれかに寄せる
    t = (title or "").lower()
    for key, name in (("method", "Methods"), ("material", "Methods"), ("experimental", "Methods"),
                      ("result", "Results"), ("discussion", "Discussion"), ("introduction", "Introduction"),
                      ("background", "Introduction"), ("conclusion", "Discussion")):
        if key in t:
            return name
    return "Other"


def _record(conn, paper_id, source, status, licence):
    # 試行の結果を記録する(同じ情報源を二度試さないため)
    conn.execute("INSERT OR REPLACE INTO fulltext(paper_id, source, status, licence, updated_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                 (paper_id, source, status, (licence or "")[:300] or None))


def _store(conn, paper, art, source, extra_licence=None):
    # JATS の <article> から、ライセンス判定→OKなら本文をセクションとして保存する。状態文字列を返す
    lic = art.find("front/article-meta/permissions/license")
    href = lic.get(XLINK) if lic is not None else None
    ltext = re.sub(r"\s+", " ", "".join(lic.itertext())).strip() if lic is not None else ""
    # XML自身にCCのURLがあればそれを優先し、無ければ Europe PMC の license 欄を使う
    if not re.search(r"creativecommons", (href or "") + ltext, re.I) and extra_licence:
        href = extra_licence
    ok = licence_ok(href, ltext)
    shown = (href or ltext or "unknown")
    body = art.find("body")
    if not ok:
        status = "licence_not_usable"
    elif body is None or not body.findall("sec"):
        status = "no_body"
    else:
        conn.execute("DELETE FROM sections WHERE paper_id=?", (paper["id"],))
        for sec in body.findall("sec"):
            text = re.sub(r"\s+", " ", " ".join(sec.itertext())).strip()
            if text:
                conn.execute("INSERT INTO sections(paper_id, name, text, parse_quality) VALUES (?,?,?,?)",
                             (paper["id"], _section_name(sec.findtext("title")), text, f"xml:{source}"))
        status = "ok"
    conn.execute("UPDATE papers SET licence=? WHERE id=? AND (licence IS NULL OR licence='unknown' OR ?)",
                 (shown[:300], paper["id"], status == "ok"))
    _record(conn, paper["id"], source, status, shown)
    conn.commit()
    return status


def fetch_pmc(conn, paper):
    # 情報源1: PubMed Central(NCBI)。PMCID があるときだけ
    root = ET.fromstring(pubmed._get("efetch.fcgi", {"db": "pmc", "id": paper["pmcid"], "retmode": "xml"}))
    art = root.find("article")
    if art is None:
        _record(conn, paper["id"], "pmc", "no_article", None)
        conn.commit()
        return "no_article"
    return _store(conn, paper, art, "pmc")


def fetch_epmc(conn, paper):
    # 情報源2: Europe PMC。記録を PMID で照会し(PMCIDやライセンスも得られる)、オープンアクセスなら全文XMLを取る
    info = europepmc.lookup(paper["pmid"]) if paper["pmid"] else None
    status = None
    if not info:
        status = "not_in_epmc"
    else:
        if info.get("pmcid") and not paper["pmcid"]:
            conn.execute("UPDATE papers SET pmcid=? WHERE id=?", (info["pmcid"], paper["id"]))  # PubMed記録に無かったPMCIDを補う
        if info.get("isOpenAccess") != "Y" or not info.get("pmcid"):
            status = "not_open_access"
    if status is None:
        text = europepmc.fulltext_xml(info["pmcid"])
        if not text:
            status = "no_fulltext"
        else:
            root = ET.fromstring(text)
            art = root if root.tag == "article" else root.find("article")
            status = "no_article" if art is None else _store(conn, paper, art, "europepmc", epmc_licence_url(info.get("license")))
            if art is not None:
                return status
    _record(conn, paper["id"], "europepmc", status, (info or {}).get("license"))
    conn.commit()
    return status


def fetch_all(conn, topic_id):
    # 採用論文のうち、まだ使える全文が無いものについて、PMC → Europe PMC の順に試す(各情報源は一度だけ)
    rows = conn.execute(
        "SELECT p.* FROM papers p JOIN screening s ON s.paper_id=p.id AND s.topic_id=? "
        "WHERE s.label='include' AND NOT EXISTS (SELECT 1 FROM sections sc WHERE sc.paper_id=p.id)", (topic_id,)).fetchall()
    stats = Counter()
    has = lambda pid: conn.execute("SELECT 1 FROM sections WHERE paper_id=?", (pid,)).fetchone() is not None
    for p in rows:
        tried = {r[0] for r in conn.execute("SELECT source FROM fulltext WHERE paper_id=?", (p["id"],))}
        for source, fn, need in (("pmc", fetch_pmc, bool(p["pmcid"])), ("europepmc", fetch_epmc, True)):
            if source in tried or not need or has(p["id"]):
                continue
            try:
                stats[f"{source}:{fn(conn, p)}"] += 1
            except Exception:  # 1本の失敗(通信など)で止めない。記録しないので次回また試す
                stats[f"{source}:error"] += 1
    return dict(stats)


def coverage(conn, topic_id):
    # 採用論文ごとに「何を読んだか」を一覧にする: 情報源、ライセンス、読んだ節、文字数、全文が無い理由
    out = []
    for p in conn.execute("SELECT p.id, p.pmid, p.title, p.pmcid, p.licence FROM papers p JOIN screening s ON s.paper_id=p.id AND s.topic_id=? "
                          "WHERE s.label='include' ORDER BY p.id", (topic_id,)).fetchall():
        secs = conn.execute("SELECT name, LENGTH(text) n, parse_quality q FROM sections WHERE paper_id=?", (p["id"],)).fetchall()
        tried = conn.execute("SELECT source, status FROM fulltext WHERE paper_id=?", (p["id"],)).fetchall()
        used = sum(min(r["n"], READ_CAPS[r["name"]]) for r in secs if r["name"] in READ_CAPS)
        out.append({"pmid": p["pmid"], "title": (p["title"] or "")[:80], "full_text": bool(secs),
                    "source": (secs[0]["q"].split(":")[-1] if secs else ""), "sections": len(secs),
                    "chars_total": sum(r["n"] for r in secs), "chars_read_for_extraction": used,
                    "licence": (p["licence"] or "")[:60],
                    "why_not": "" if secs else ("; ".join(f"{r['source']}:{r['status']}" for r in tried) or "not tried")})
    return out


def show(conn, pmid, chars=400):
    # 1本の論文の全文セクションを確認する(節名、文字数、冒頭、抽出で読む範囲かどうか)
    rows = conn.execute("SELECT s.name, s.text, s.parse_quality FROM sections s JOIN papers p ON p.id=s.paper_id WHERE p.pmid=?", (str(pmid),)).fetchall()
    return [{"section": r["name"], "chars": len(r["text"]), "read_for_extraction": r["name"] in READ_CAPS,
             "source": r["parse_quality"], "start": r["text"][:chars]} for r in rows]
