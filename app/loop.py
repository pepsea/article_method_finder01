"""知識駆動ループ: 収集 → 重複除外 → 判定 → 全文 → 抽出 → 統合 → 目標チェック → 知識から次の検索語 → 繰り返し。"""
import json

from app import design, export, fulltext, knowledge, stages


def run_loop(conn, topic_id, rounds=2, extract_limit=200, screen_limit=500):
    # 1ラウンドごとに結果をフォルダに保存する(履歴ページから見られる)。新しい検索語が出なければ収束として止める
    history = []
    for rnd in range(1, rounds + 1):
        print(f"=== round {rnd} ===", flush=True)
        print("collect", stages.collect(conn, topic_id)["added"], "new papers", flush=True)
        print("dedup", stages.dedup(conn)["merged"], "merged", flush=True)
        print("screen", stages.screen(conn, topic_id, limit=screen_limit), flush=True)
        print("fulltext", fulltext.fetch_all(conn, topic_id), flush=True)
        print("extract", knowledge.extract_all(conn, topic_id, extract_limit), flush=True)
        syn = knowledge.synthesize(conn, topic_id)
        goal = knowledge.check_goal(conn, topic_id, syn)
        folder = export.export_search(conn, topic_id)
        knowledge.write_report(conn, topic_id, folder, syn, goal, rnd)
        print('summary', design.write_summary(conn, topic_id, folder, syn, goal, rnd), flush=True)
        (folder / "goal_check.json").write_text(json.dumps(goal, indent=2, ensure_ascii=False))
        passed = sum(v["ok"] for v in goal.values())
        print(f"round {rnd}: goal {passed}/{len(goal)} passed; saved {folder}", flush=True)
        for k, v in goal.items():
            print(f"  {'PASS' if v['ok'] else 'FAIL'} {k}: {v['detail']}", flush=True)
        history.append({"round": rnd, "passed": passed, "folder": str(folder)})
        new = knowledge.knowledge_queries(conn, topic_id, syn)
        print("new queries from knowledge:", new, flush=True)
        if not new:
            print("converged: no new queries", flush=True)
            break
    return history
