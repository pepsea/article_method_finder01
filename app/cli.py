"""CLI: python -m app.cli <command>。各ステージを1コマンドで実行する。"""
import argparse
import json

from app import stages
from app.db import init_db


def main():
    # コマンド名と引数を読み取り、対応するステージを呼ぶ
    p = argparse.ArgumentParser(prog="app.cli")
    p.add_argument("cmd", choices=["init-db", "rebuild", "create-topic", "keywords", "collect", "recall", "list-papers", "dedup", "set-criteria", "screen", "screen-summary", "export", "fulltext", "extract", "report", "loop", "show-profile", "show-criteria", "list-topics", "set-profile", "summary", "index", "ask", "taxonomy", "from-question", "design-queries", "queries-table"])
    p.add_argument("--topic", type=int)
    p.add_argument("--name")
    p.add_argument("--file")
    p.add_argument("--question")
    p.add_argument("--expand", action="store_true", help="with ask: if the knowledge base lacks verified evidence, collect more papers from the question and ask again")
    p.add_argument("--profile-file", help="edited profile JSON to use instead of generating one")
    p.add_argument("--limit", type=int)
    p.add_argument("--model")
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--seeds", help="semicolon-separated seed queries")
    a = p.parse_args()
    conn = init_db()

    if a.cmd in ("init-db", "rebuild"):
        # rebuild は M1 時点では DB 作成のみ(派生データはまだ無い)
        print("DB ready")
    elif a.cmd == "create-topic":
        # トピック作成 + プロファイルと基準の自動下書き(--file で自分の基準、--profile-file で編集済みプロファイルを使える)
        crit = open(a.file).read() if a.file else None
        tid = stages.setup_topic(conn, a.name, [s.strip() for s in a.seeds.split(";")], crit, a.profile_file)
        print("topic id:", tid)
    elif a.cmd == "list-topics":
        for r in conn.execute("SELECT id, name FROM topics ORDER BY id"):
            print(r["id"], r["name"])
    elif a.cmd == "summary":
        # サマリーレポート(Gemma設計の雛形 + 検証済みの内容)を新しい日時フォルダに保存する
        from app import design, export, knowledge
        syn = knowledge.synthesize(conn, a.topic)
        goal = knowledge.check_goal(conn, a.topic, syn)
        folder = export.export_search(conn, a.topic)
        knowledge.write_report(conn, a.topic, folder, syn, goal, "summary")
        print(design.write_summary(conn, a.topic, folder, syn, goal, "summary"))
    elif a.cmd == "index":
        from app import rag
        print(rag.build_index(conn, a.topic))
    elif a.cmd == "design-queries":
        # 検索語の設計: 定義 → 候補 → 試験 → 決定(--question で新しいトピック、--topic で既存トピックの再設計)
        from app import querydesign as qd
        if a.question:
            defn = qd.define(a.question)
            print(json.dumps(defn, indent=2, ensure_ascii=False))
            tid = qd.new_topic(conn, defn)
            print("topic id:", tid)
        else:
            tid = a.topic
        for r in qd.design(conn, tid):
            print(f"[{r['status']:7}] {r['count']:6d} hits  precision={r['precision']}  novelty={r['novelty']}  {r['query']}\n          -> {r['reason']}")
    elif a.cmd == "queries-table":
        from app import querydesign as qd
        for r in qd.table(conn, a.topic):
            print(f"[{r['status'] or 'kept':7}] {r['origin']:8} {r['count'] or 0:6d}  {r['query']}")
    elif a.cmd == "from-question":
        # 質問からPubMed検索式を作り(件数で検証)、トピックを登録する
        from app import questions
        tid, res = questions.topic_from_question(conn, a.question)
        print("topic id:", tid, "|", res["topic_name"])
        for q in res["queries"]:
            print(f"  [{q['status']:10}] {q['count']:6d}  {q['query']}")
    elif a.cmd == "ask":
        from app import questions, rag
        out = questions.ask_with_expansion(conn, a.topic, a.question, expand="auto" if a.expand else False)
        if out["expanded"]:
            print("expanded:", out["expanded"])
        r = out["final"]
        print(r["answer"])
        for e in r["evidence"]:
            print(f"  [{e['n']}] PMID {e['pmid']} {'verified' if e['verified'] else 'NOT verified'}: {e['quote']}")
    elif a.cmd == "taxonomy":
        from app import taxonomy
        print(taxonomy.tag_papers(conn, a.topic))
    elif a.cmd == "set-profile":
        # 編集したプロファイルJSONをトピックに反映する(版 schema_version を変えると抽出をやり直す)
        from app import profile as prof
        newp = json.load(open(a.file))
        errs = prof.validate(newp)
        print("\n".join(errs) if errs else f"saved {prof.save(conn, a.topic, newp)}")
    elif a.cmd == "show-profile":
        from app import profile as prof
        print(json.dumps(prof.load(conn, a.topic), indent=2, ensure_ascii=False))
    elif a.cmd == "show-criteria":
        print(conn.execute("SELECT criteria FROM topics WHERE id=?", (a.topic,)).fetchone()["criteria"])
    elif a.cmd == "keywords":
        print(json.dumps(stages.generate_keywords(conn, a.topic), indent=2, ensure_ascii=False))
    elif a.cmd == "collect":
        print(json.dumps(stages.collect(conn, a.topic), indent=2, ensure_ascii=False))
    elif a.cmd == "recall":
        name = conn.execute("SELECT name FROM topics WHERE id=?", (a.topic,)).fetchone()["name"]
        print(json.dumps(stages.recall(conn, name, a.topic), indent=2))
    elif a.cmd == "dedup":
        r = stages.dedup(conn)
        print({"checked": r["checked"], "merged": r["merged"]})
    elif a.cmd == "set-criteria":
        # 基準テキストをファイルから読んで topics.criteria に保存する
        conn.execute("UPDATE topics SET criteria=? WHERE id=?", (open(a.file).read(), a.topic))
        conn.commit()
        print("criteria saved")
    elif a.cmd == "screen":
        print(json.dumps(stages.screen(conn, a.topic, a.limit, a.model, a.workers), indent=2))
    elif a.cmd == "screen-summary":
        print(json.dumps(stages.screen_summary(conn, a.topic), indent=2))
    elif a.cmd == "export":
        from app import export
        print("saved to", export.export_search(conn, a.topic))
    elif a.cmd == "fulltext":
        from app import fulltext
        print(fulltext.fetch_all(conn, a.topic))
    elif a.cmd == "extract":
        from app import knowledge
        print(knowledge.extract_all(conn, a.topic, a.limit))
    elif a.cmd == "report":
        # 現時点の知識から統合・目標チェック・レポートを作り、新しい日時フォルダに保存する
        from app import export, knowledge
        syn = knowledge.synthesize(conn, a.topic)
        goal = knowledge.check_goal(conn, a.topic, syn)
        folder = export.export_search(conn, a.topic)
        knowledge.write_report(conn, a.topic, folder, syn, goal, "re-synthesis (revised rules)")
        for k, v in goal.items():
            print("PASS" if v["ok"] else "FAIL", k, "-", v["detail"])
        print("saved", folder)
    elif a.cmd == "loop":
        from app import loop
        loop.run_loop(conn, a.topic, a.rounds, a.limit or 250)
    elif a.cmd == "list-papers":
        for r in conn.execute("SELECT pmid, year, title FROM papers ORDER BY pmid"):
            print(r["pmid"], r["year"], r["title"])


if __name__ == "__main__":
    main()
