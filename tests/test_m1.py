"""M1 テスト(通信なし): XML解析と recall 計算。"""
import xml.etree.ElementTree as ET

from app import pubmed, stages
from app.db import init_db

# 手書きの最小 PubMed XML(実在論文ではなく、解析ロジック確認用のダミー)
XML = """<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
<Journal><Title>J Test</Title><JournalIssue><PubDate><MedlineDate>2020 Jan-Feb</MedlineDate></PubDate></JournalIssue></Journal>
<ArticleTitle>Dummy title</ArticleTitle><Abstract><AbstractText>A.</AbstractText><AbstractText>B.</AbstractText></Abstract>
<AuthorList><Author><LastName>Doe</LastName><Initials>J</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Retracted Publication</PublicationType></PublicationTypeList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/ABC</ArticleId></ArticleIdList></PubmedData></PubmedArticle>"""


def test_parse_article():
    # 年(MedlineDate)・DOI小文字化・要旨結合・撤回フラグが正しく取れる
    p = pubmed._parse(ET.fromstring(XML))
    assert p["year"] == 2020 and p["doi"] == "10.1/abc"
    assert p["abstract"] == "A. B." and p["is_retracted"] == 1 and p["authors"] == "Doe J"


def test_recall_unmeasured_without_golden(tmp_path, monkeypatch):
    # ゴールデンセットが無い時は、数値を作らず「未測定」を返す
    monkeypatch.chdir(tmp_path)
    r = stages.recall(init_db(":memory:"), "none")
    assert r["recall"] is None


def test_recall_computed(tmp_path, monkeypatch):
    # 3件中2件が収集済みなら recall=2/3
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests/golden").mkdir(parents=True)
    (tmp_path / "tests/golden/t.csv").write_text("pmid\n1\n2\n3\n")
    conn = init_db(":memory:")
    conn.execute("INSERT INTO papers(pmid) VALUES ('1'),('2')")
    r = stages.recall(conn, "t")
    assert r["found"] == 2 and r["missing"] == ["3"]
