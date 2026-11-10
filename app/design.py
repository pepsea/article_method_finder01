"""サマリーレポートのウェブデザイン。ローカルLLM(既定 gemma4:12b)が「HTML/CSSの雛形」だけを設計し、内容(数値・引用)はPythonが検証済みのデータで埋める。
モデルが内容を作らないので、数値や引用を捏造できない。雛形は検査(スクリプト・外部通信なし、必須の差し込み位置あり)してキャッシュし、同じ雛形を再利用する。"""
import hashlib
import json
import re
from html import escape
from pathlib import Path

import httpx

from app import knowledge, stages
from app.config import load_config

DESIGN_DIR = Path("data/design")
PROMPT_VERSION = "design-v1"
REQUIRED = ["TITLE", "SUBTITLE", "FINDINGS", "GLANCE", "GOAL", "LIMITS", "SEARCH", "LINKS"]
OPTIONAL = ["DETAILS", "MATRIX", "CONFLICTS", "TAXONOMY", "REFERENCES"]

BRIEF = """You are a senior web designer. Design ONE self-contained HTML5 page template for a one-page SUMMARY REPORT of a literature-mining result
(the best analytical method per condition, with evidence). Readers are researchers: calm, clear, scientific, information-dense but easy to scan.

HARD RULES
- Output ONLY the HTML document (no explanation, no markdown fences).
- Put all CSS in one <style> element. No <script>, no external files, no web fonts, no images, no http(s) URLs anywhere. Use system fonts.
- Responsive (works at 375px and at 1200px width) with <meta name="viewport" content="width=device-width,initial-scale=1">, and support dark mode with @media (prefers-color-scheme: dark).
- Flat, restrained colours (one accent colour, neutral greys). No gradients, no shadows heavier than 1px, no decorative animation.
- Do NOT write any report content. Use exactly these placeholders where content goes, each written like {{NAME}} on its own:
  {{TITLE}} (text, goes inside <h1> and <title>), {{SUBTITLE}} (one line of text), {{FINDINGS}} (a <ul> of the 3 headline findings; show it prominently near the top, e.g. as a highlighted panel),
  {{GLANCE}} (a wide <table> of results; make it scroll horizontally on small screens by wrapping it in a div with overflow-x:auto), {{GOAL}} (a <ul> of checks), {{LIMITS}} (a <ul> of caveats),
  {{SEARCH}} (a <ul> of counts), {{LINKS}} (a short paragraph with links).
- The injected content uses these class names, so style them: table, th, td, a, ul, li, .g (evidence grade badge; .gA .gB .gC .gD = green to grey), .badge (.ok green / .ng red, white text), .muted, q.
- Suggested structure: a header band with title and subtitle; a highlighted headline-findings panel; the results table; then two columns on wide screens (goal checks | limitations); then the search record and links as a footer.
"""


def validate(html):
    # 雛形の検査。問題の一覧を返す(空なら使える)
    errs = []
    for name in REQUIRED:
        if "{{" + name + "}}" not in html:
            errs.append(f"missing placeholder {{{{{name}}}}}")
    if re.search(r"<script|javascript:|<iframe|<object|<embed|<link\b|@import|\bon[a-z]+\s*=", html, re.I):
        errs.append("contains script, embed, link, @import or inline event handler")
    if re.search(r"https?://|//[a-z0-9.-]+\.[a-z]{2,}", html, re.I):
        errs.append("contains an external URL")
    if "<style" not in html.lower():
        errs.append("no <style> element")
    if "viewport" not in html.lower():
        errs.append("no viewport meta")
    if not (1500 <= len(html) <= 60000):
        errs.append(f"size {len(html)} bytes is out of range")
    return errs


def clean(text):
    # モデル出力から ```html フェンスや前置きを取り除き、<!doctype>〜</html> だけを残す
    t = text.strip()
    t = re.sub(r"^```[a-z]*\n|\n```$", "", t)
    m = re.search(r"<!doctype html.*</html>", t, re.I | re.S)
    return m.group(0) if m else t


class ModelMissing(Exception):
    pass


def ask_model(cfg, model, prompt):
    # Ollama にHTMLを書かせる。モデルが入っていなければ ModelMissing
    r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=1800, json={
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.4, "seed": 0, "num_ctx": 8192, "num_predict": 6000}})
    if r.status_code == 404:
        raise ModelMissing(model)
    r.raise_for_status()
    return r.json()["response"]


def builtin_template():
    # モデルが無い/失敗したときの既定デザイン(同じ差し込み位置を持つ)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{{{TITLE}}}}</title><style>{knowledge.DEFAULT_CSS}
header{{background:var(--card);border-bottom:1px solid var(--line);padding:20px 16px}} header>div,main{{max-width:1000px;margin:0 auto}}
.panel{{background:var(--card);border:1px solid var(--line);border-left:4px solid #1a5fb4;border-radius:6px;padding:4px 18px;margin:20px 0}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:24px}} @media (max-width:720px){{.cols{{grid-template-columns:1fr}}}}</style></head>
<body><header><div><h1>{{{{TITLE}}}}</h1><p class="muted">{{{{SUBTITLE}}}}</p></div></header><main>
<h2>Headline findings</h2><div class="panel">{{{{FINDINGS}}}}</div>
<h2>Best method per condition</h2><div class="scroll">{{{{GLANCE}}}}</div>
<div class="cols"><div><h2>Goal check</h2>{{{{GOAL}}}}</div><div><h2>Limitations</h2>{{{{LIMITS}}}}</div></div>
<h2>Search record</h2>{{{{SEARCH}}}}<p>{{{{LINKS}}}}</p></main></body></html>"""


def get_template(cfg=None, model=None, force=False):
    # 雛形を返す: キャッシュ → Gemma に設計させる(最大3回、検査の問題を伝えて直させる) → だめなら既定デザイン。(html, 取得元) を返す
    cfg = cfg or load_config()
    model = model or cfg.get("design_model") or "gemma4:12b"
    key = hashlib.sha1((model + PROMPT_VERSION + BRIEF).encode()).hexdigest()[:10]
    path = DESIGN_DIR / f"summary_{re.sub(r'[^a-z0-9]+', '-', model.lower())}_{key}.html"
    if path.exists() and not force and not validate(path.read_text()):
        return path.read_text(), f"cached:{model}"
    prompt = BRIEF
    for _ in range(3):
        try:
            html = clean(ask_model(cfg, model, prompt))
        except ModelMissing:
            return builtin_template(), f"builtin (model {model} is not installed: run `ollama pull {model}`)"
        except httpx.HTTPError as e:
            return builtin_template(), f"builtin (model call failed: {e})"
        errs = validate(html)
        if not errs:
            DESIGN_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(html)
            return html, f"generated:{model}"
        prompt = BRIEF + "\n\nYour previous HTML was rejected for these reasons. Fix them and output the full HTML again:\n- " + "\n- ".join(errs)
    return builtin_template(), f"builtin (model {model} output failed validation: {'; '.join(errs)})"


def render(template, parts, title, subtitle, links):
    # 雛形の {{名前}} を、検証済みの内容(HTML断片)で置き換える。使われなかった任意の差し込み位置は空にする
    values = {"TITLE": escape(title), "SUBTITLE": escape(subtitle), "LINKS": links, "FINDINGS": parts["findings"], "GLANCE": parts["glance"],
              "GOAL": parts["goal"], "LIMITS": parts["limits"], "SEARCH": parts["search"], "DETAILS": parts["details"], "MATRIX": parts["matrix"],
              "CONFLICTS": parts["conflicts"], "TAXONOMY": parts["taxonomy"], "REFERENCES": parts["references"]}
    return re.sub(r"\{\{([A-Z]+)\}\}", lambda m: values.get(m.group(1), ""), template)


def write_summary(conn, topic_id, folder, syn, goal, run_label, model=None):
    # サマリーレポート(summary.html)を書く。デザインはGemma(または既定)、内容は report_parts の検証済みデータ。使った雛形の取得元を返す
    cfg = load_config()
    template, source = get_template(cfg, model)
    parts = knowledge.report_parts(conn, topic_id, syn, goal, run_label)
    title = conn.execute("SELECT name FROM topics WHERE id=?", (topic_id,)).fetchone()["name"]
    links = "<a href='report.html'>Full report</a> &middot; <a href='included.csv'>Included papers (CSV)</a> &middot; <a href='queries.csv'>Search queries (CSV)</a>"
    html = render(template, parts, f"Summary: {title}", f"{syn['n_usable']} papers with usable knowledge · run: {run_label}", links)
    (folder / "summary.html").write_text(html)
    stages.new_run(conn, "design", model or cfg.get("design_model"), None, {"template_source": source}, notes=str(folder / "summary.html"))
    return {"path": str(folder / "summary.html"), "design_source": source}
