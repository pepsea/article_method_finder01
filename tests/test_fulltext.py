"""全文取得のテスト(通信なし): ライセンス判定・Europe PMC の補完・情報源の順序・再抽出。"""
import xml.etree.ElementTree as ET

from app import fulltext as ft, knowledge as k
from app.db import init_db

ART = """<article><front><article-meta>{perm}</article-meta></front><body>
<sec><title>Methods</title><p>Urine was diluted.</p></sec><sec><title>Results</title><p>LLOQ was 0.1 ng/mL.</p></sec></body></article>"""
CC_BY = "<permissions><license xmlns:xlink='http://www.w3.org/1999/xlink' xlink:href='https://creativecommons.org/licenses/by/4.0/'>CC BY</license></permissions>"
CC_NC = "<permissions><license xmlns:xlink='http://www.w3.org/1999/xlink' xlink:href='https://creativecommons.org/licenses/by-nc/4.0/'>NC</license></permissions>"


def _paper(conn):
    conn.execute("INSERT INTO papers(id, pmid, pmcid, title) VALUES (1,'11','PMC1','t')")
    return conn.execute("SELECT * FROM papers WHERE id=1").fetchone()


def test_epmc_licence_mapping():
    # Europe PMC の license 欄をCC URLにして、商用利用できるものだけ通す
    assert ft.licence_ok(ft.epmc_licence_url("cc by"), "") and ft.licence_ok(ft.epmc_licence_url("cc by-sa"), "")
    assert ft.licence_ok(ft.epmc_licence_url("CC0"), "")
    assert not ft.licence_ok(ft.epmc_licence_url("cc by-nc"), "") and not ft.licence_ok(ft.epmc_licence_url("cc by-nd"), "")
    assert ft.epmc_licence_url(None) is None and not ft.licence_ok(ft.epmc_licence_url(None), "")


def test_licence_from_plain_text_without_url():
    # URLが無い文章でも「Creative Commons Attribution」だけなら可。非営利・改変禁止の語があれば不可
    assert ft.licence_ok(None, "Open Access This article is licensed under a Creative Commons Attribution 4.0 International License")
    assert ft.licence_ok(None, "distributed under the terms of the Creative Commons Attribution License")
    assert not ft.licence_ok(None, "Creative Commons Attribution-NonCommercial License")
    assert not ft.licence_ok(None, "Creative Commons Attribution-NoDerivatives 4.0")
    assert not ft.licence_ok(None, "available via the PMC Open Access Subset for unrestricted noncommercial use")


def test_store_uses_epmc_licence_when_xml_has_none():
    # XMLに許諾情報が無くても、Europe PMC が cc by と言えば本文を保存し、取得元を記録する
    conn = init_db(":memory:"); p = _paper(conn)
    st = ft._store(conn, p, ET.fromstring(ART.format(perm="")), "europepmc", ft.epmc_licence_url("cc by"))
    assert st == "ok"
    assert conn.execute("SELECT parse_quality FROM sections WHERE paper_id=1 AND name='Methods'").fetchone()[0] == "xml:europepmc"


def test_store_rejects_non_commercial_and_unknown():
    # NCは保存しない。XMLにもEurope PMCにも許諾が無ければ保存しない
    conn = init_db(":memory:"); p = _paper(conn)
    assert ft._store(conn, p, ET.fromstring(ART.format(perm=CC_NC)), "pmc") == "licence_not_usable"
    assert ft._store(conn, p, ET.fromstring(ART.format(perm="")), "europepmc", None) == "licence_not_usable"
    assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 0
    assert ft._store(conn, p, ET.fromstring(ART.format(perm=CC_BY)), "pmc") == "ok"


def test_fetch_all_order_and_no_retry(monkeypatch):
    # PMC → Europe PMC の順に試し、PMCで取れたらEurope PMCは呼ばない。試した情報源は再試行しない
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name) VALUES (1,'t')")
    for i in (1, 2):
        conn.execute("INSERT INTO papers(id, pmid, pmcid) VALUES (?,?,?)", (i, str(i), f"PMC{i}"))
        conn.execute("INSERT INTO screening(paper_id, topic_id, label, reviewer) VALUES (?,1,'include','llm')", (i,))
    calls = []
    def pmc(c, p):
        calls.append(("pmc", p["id"]))
        if p["id"] == 1:
            c.execute("INSERT INTO sections(paper_id, name, text) VALUES (1,'Methods','x')")
            return "ok"
        ft._record(c, p["id"], "pmc", "licence_not_usable", None)
        return "licence_not_usable"
    def epmc(c, p):
        calls.append(("epmc", p["id"])); ft._record(c, p["id"], "europepmc", "not_open_access", None); return "not_open_access"
    monkeypatch.setattr(ft, "fetch_pmc", pmc); monkeypatch.setattr(ft, "fetch_epmc", epmc)
    stats = ft.fetch_all(conn, 1)
    assert calls == [("pmc", 1), ("pmc", 2), ("epmc", 2)]   # 論文1はPMCで取れたのでEurope PMCは呼ばない
    assert stats.get("europepmc:not_open_access") == 1
    calls.clear(); ft.fetch_all(conn, 1)
    assert calls == []   # 試した情報源は二度と呼ばない


def test_extraction_is_redone_when_fulltext_arrives_later(monkeypatch):
    # 抽出の後で全文が取れた論文は、古い抽出を置き換えて再抽出される(1件だけ残る)
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name) VALUES (1,'t')")
    conn.execute("INSERT INTO papers(id, pmid, title, abstract) VALUES (1,'1','t','a')")
    conn.execute("INSERT INTO screening(paper_id, topic_id, label, reviewer) VALUES (1,1,'include','llm')")
    conn.execute("INSERT INTO runs(id, stage, started_at) VALUES (1,'extract','2026-01-01 00:00:00')")
    conn.execute("INSERT INTO extractions(paper_id, schema_version, json, status, run_id) VALUES (1,'1.1','{}','insufficient',1)")
    conn.execute("INSERT INTO fulltext(paper_id, source, status, updated_at) VALUES (1,'europepmc','ok','2026-06-01 00:00:00')")
    conn.execute("INSERT INTO topic_papers VALUES (1,1)")
    empty, _ = k.verify_fields({}, [("Abstract", "a")])
    monkeypatch.setattr(k.httpx, "get", lambda *a, **kw: type("R", (), {"json": lambda s: {"models": []}})())
    monkeypatch.setattr(k, "extract_one", lambda cfg, c, p, P: (empty, 0))
    stats = k.extract_all(conn, 1)
    assert stats.get("re_extracted") == 1
    assert conn.execute("SELECT COUNT(*) FROM extractions WHERE paper_id=1").fetchone()[0] == 1
