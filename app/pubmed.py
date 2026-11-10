"""PubMed (NCBI E-utilities) クライアント。全レスポンスを data/raw/pubmed/ にキャッシュする。"""
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
CACHE_DIR = Path(os.environ.get("RAW_DIR", "data/raw")) / "pubmed"

# APIキーがあれば 10件/秒、無ければ 3件/秒までなので、その範囲に収まる待ち時間を決める
API_KEY = os.environ.get("NCBI_API_KEY") or None
EMAIL = os.environ.get("NCBI_EMAIL") or None
MIN_INTERVAL = 0.11 if API_KEY else 0.36
_last_call = 0.0


def _get(endpoint, params):
    # キャッシュにあればそれを返す(再実行で同じ結果になる=S4の土台)。無ければ取得して保存
    global _last_call
    key = hashlib.sha1((endpoint + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    ext = "xml" if params.get("retmode") == "xml" else "json"
    path = CACHE_DIR / endpoint.replace(".fcgi", "") / f"{key}.{ext}"
    if path.exists():
        return path.read_text()
    # キャッシュに無い時だけ、レート制限を守って通信する(APIキーはキャッシュキーに含めない)
    wait = MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    full = dict(params)
    if API_KEY:
        full["api_key"] = API_KEY
    if EMAIL:
        full["email"] = EMAIL
    full["tool"] = "method_finder"
    r = httpx.get(BASE + endpoint, params=full, timeout=60)
    _last_call = time.time()
    r.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(r.text)
    return r.text


def search(term, end_date, retmax=0, retstart=0):
    # 検索。end_date(YYYY/MM/DD)で上限を固定し、後日の再実行でも同じ件数・同じIDになるようにする
    text = _get("esearch.fcgi", {
        "db": "pubmed", "term": term, "retmode": "json", "retmax": retmax, "retstart": retstart,
        "datetype": "pdat", "mindate": "1900/01/01", "maxdate": end_date, "sort": "pub_date",
    })
    res = json.loads(text)["esearchresult"]
    return {"count": int(res.get("count", 0)), "ids": res.get("idlist", []),
            "translation": res.get("querytranslation", "")}


def fetch(pmids):
    # PMIDのリストから論文メタデータ(XML)を取得して辞書のリストにする
    out = []
    for i in range(0, len(pmids), 200):
        batch = pmids[i:i + 200]
        text = _get("efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
        root = ET.fromstring(text)
        for art in root.findall("PubmedArticle"):
            out.append(_parse(art))
    return out


def _parse(art):
    # 1本分の PubmedArticle XML から、papers テーブルに入れる項目を取り出す
    mc = art.find("MedlineCitation")
    a = mc.find("Article")
    pmid = mc.findtext("PMID")
    title = "".join(a.find("ArticleTitle").itertext()) if a.find("ArticleTitle") is not None else None
    abstract = " ".join("".join(t.itertext()) for t in a.findall("Abstract/AbstractText")) or None
    year = a.findtext("Journal/JournalIssue/PubDate/Year")
    if not year:
        md = a.findtext("Journal/JournalIssue/PubDate/MedlineDate") or ""
        year = md[:4] if md[:4].isdigit() else None
    authors = "; ".join(
        f"{x.findtext('LastName')} {x.findtext('Initials')}"
        for x in a.findall("AuthorList/Author") if x.findtext("LastName"))
    doi = pmcid = None
    for i in art.findall("PubmedData/ArticleIdList/ArticleId"):
        if i.get("IdType") == "doi":
            doi = (i.text or "").lower()
        elif i.get("IdType") == "pmc":
            pmcid = i.text
    ptypes = [p.text for p in a.findall("PublicationTypeList/PublicationType")]
    return {
        "pmid": pmid, "doi": doi, "pmcid": pmcid, "title": title, "abstract": abstract,
        "year": int(year) if year else None, "journal": a.findtext("Journal/Title"),
        "authors": authors or None, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "is_retracted": int("Retracted Publication" in ptypes),
    }
