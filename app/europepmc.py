"""Europe PMC REST クライアント: 論文のライセンス/PMCIDの照会と、オープンアクセス全文(JATS XML)の取得。全応答をキャッシュする。"""
import hashlib
import json
import os
import time
from pathlib import Path

import httpx

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
CACHE_DIR = Path(os.environ.get("RAW_DIR", "data/raw")) / "europepmc"
MIN_INTERVAL = 0.2  # 礼儀として 5件/秒以下
_last = 0.0


def _get(path, params=None):
    # キャッシュにあればそれを返す。無ければ取得して保存する。404は「無い」として空ファイルで記録(再問い合わせしない)
    global _last
    key = hashlib.sha1((path + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    f = CACHE_DIR / f"{key}.txt"
    if f.exists():
        return f.read_text() or None
    wait = MIN_INTERVAL - (time.time() - _last)
    if wait > 0:
        time.sleep(wait)
    r = httpx.get(BASE + path, params=params, timeout=60, follow_redirects=True)
    _last = time.time()
    if r.status_code == 404:
        text = ""
    else:
        r.raise_for_status()
        text = r.text
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return text or None


def lookup(pmid):
    # PMIDから Europe PMC の記録(pmcid, license, isOpenAccess など)を返す。無ければ None
    text = _get("search", {"query": f"EXT_ID:{pmid} AND SRC:MED", "resultType": "core", "format": "json", "pageSize": 1})
    if not text:
        return None
    res = json.loads(text).get("resultList", {}).get("result", [])
    return res[0] if res else None


def fulltext_xml(pmcid):
    # オープンアクセス全文のJATS XMLを返す(取得できなければ None)
    return _get(f"{pmcid}/fullTextXML")
