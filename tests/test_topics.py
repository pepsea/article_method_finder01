"""複数トピック対応のテスト(通信なし): 論文のトピック分離とプロファイル。"""
import json

from app import export, knowledge as k, profile as prof, stages
from app.db import init_db


def _paper(conn, pmid, title, topic_ids):
    # 論文を1件入れ、指定トピックに紐づける
    cur = conn.execute("INSERT INTO papers(pmid, title, abstract) VALUES (?,?,?)", (pmid, title, "abstract about " + title))
    for t in topic_ids:
        conn.execute("INSERT INTO topic_papers VALUES (?,?)", (t, cur.lastrowid))


def test_topics_do_not_see_each_others_papers():
    # トピック2は、トピック1が集めた論文を判定・集計の対象にしない
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name, criteria) VALUES (1,'A','c'),(2,'B','c')")
    _paper(conn, "1", "nicotine paper", [1])
    _paper(conn, "2", "other paper", [2])
    _paper(conn, "3", "shared nicotine paper", [1, 2])
    assert export.counts(conn, 1)["collected"] == 2
    assert export.counts(conn, 2)["collected"] == 2
    assert {r["pmid"] for r in conn.execute(export.PAPER_SQL, (2, 2))} == {"2", "3"}


def test_profile_validation_and_generic_fallback():
    # 組み込み・汎用プロファイルは検証を通り、壊れた正規表現は検出される
    assert prof.validate(prof.NICOTINE) == []
    assert prof.validate(prof.generic("Some topic")) == []
    bad = json.loads(json.dumps(prof.NICOTINE)); bad["entity_regex"] = "(unclosed"; bad["gap_query_template"] = "no placeholder"
    errs = prof.validate(bad)
    assert any("entity_regex" in e for e in errs) and any("gap_query_template" in e for e in errs)


def test_profile_drives_extraction_names_and_classes():
    # LLMには役割の表示名で答えさせ、保存は役割キーに戻る。分類規則もプロファイルから
    P = json.loads(json.dumps(prof.NICOTINE))
    P["roles"]["sample_preparation"]["name"] = "alignment_algorithm"
    raw = {"alignment_algorithm": {"value": "STAR", "quote": "reads were aligned with STAR"}}
    out, rejected = k.verify_fields(raw, [("Abstract", "Reads were aligned with STAR v2.7.")], P)
    assert out["sample_preparation"]["value"] == "STAR" and out["sample_preparation"]["verified"]
    assert k.classify("aligned with STAR", [["splice-aware aligner", "star|hisat"]]) == "splice-aware aligner"


def test_goal_check_omits_sanity_when_profile_has_none(tmp_path, monkeypatch):
    # sanity が null のプロファイルでは、トピック固有の G5 を出さない
    conn = init_db(":memory:")
    P = prof.generic("X"); conn.execute("INSERT INTO topics(id, name, profile) VALUES (1,'X',?)", (json.dumps(P),))
    syn = {"n_usable": 0, "recs": {}, "cells": {}}
    goal = k.check_goal(conn, 1, syn)
    assert not any(key.startswith("G5") for key in goal)


def test_legacy_topic_gets_nicotine_profile():
    # profile が無い(旧)トピックは組み込みのニコチン版になる
    conn = init_db(":memory:")
    conn.execute("INSERT INTO topics(id, name) VALUES (1,'old')")
    assert prof.load(conn, 1)["schema_version"] == "1.1"
