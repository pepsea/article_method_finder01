"""M3-M7 テスト(通信なし): 引用検証・ライセンス・分類・LLOQ・証拠グレード。"""
from app import fulltext, knowledge as k


def test_quote_verification_is_loose_but_real():
    # 空白・大文字小文字の違いは許し、原文に無い引用は却下する
    src = [("Abstract", "Internal standards were Nic-d3 and Cot-d3 in urine.")]
    raw = {"internal_standard": {"value": "Nic-d3, Cot-d3", "quote": "Nic-d 3 and COT-d3"},
           "matrix": {"value": "urine", "quote": "made-up sentence"},
           "lloq": {"value": "not stated", "quote": None},
           "validation_type": {"value": "internal", "quote": None}}
    out, rejected = k.verify_fields(raw, src)
    assert out["internal_standard"]["verified"] and out["internal_standard"]["location"] == "Abstract"
    assert out["matrix"]["value"] is None and rejected == 1       # 捏造された引用は却下
    assert out["lloq"]["value"] is None                            # "not stated" は null
    assert out["validation_type"]["inferred"] and out["validation_type"]["value"] == "internal"


def test_licence_only_free_cc():
    assert fulltext.licence_ok(None, "https://creativecommons.org/licenses/by/4.0/ Licensee MDPI")
    assert not fulltext.licence_ok(None, "https://creativecommons.org/licenses/by-nc/4.0/")
    assert not fulltext.licence_ok(None, "https://creativecommons.org/licenses/by-nd/4.0/")
    assert not fulltext.licence_ok(None, "This file is available for text mining.")


def test_classifiers_and_lloq():
    assert k.classify("protein precipitation with acetonitrile", k.PREP_RULES) == "protein precipitation"
    assert k.classify("Oasis MCX solid-phase extraction", k.PREP_RULES) == "solid-phase extraction"
    assert k.classify("Accucore C18 150 mm", k.COLUMN_RULES) == "C18"
    assert k.matrix_classes("human plasma and urine") == ["urine", "plasma/serum/blood"]
    assert k.parse_lloq("0.22 pg mL-1 for nicotine") is None or k.parse_lloq("0.5 ng/mL") == (0.5, "ng/ml")
    assert k.parse_lloq("0.010 µg/mL") == (0.01, "µg/ml")


def test_grade():
    # 改訂グレード: 数値つき検証(lloq)を出した独立グループ数で決まる
    mk = lambda g, lloq=(1, "ng/ml"), pre=0: {"group": g, "pmid": g, "lloq": lloq, "preprint": pre}
    assert k.grade([mk("a")]) == "C"
    assert k.grade([mk("a"), mk("b")]) == "B"
    assert k.grade([mk("a"), mk("b"), mk("c")]) == "A"
    assert k.grade([mk("a", None)]) == "D"          # 数値つき検証なし
    assert k.grade([mk("a", pre=1), mk("b", pre=1)]) == "D"
