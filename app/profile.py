"""トピックごとの「プロファイル」: 何を知識として抽出し、どう分類し、どう追加検索するか。LLMが新トピック用に自動生成し、人が編集できる。"""
import json
import re
from pathlib import Path

import httpx

from app.config import load_config

PROFILE_DIR = Path("profiles")
# 抽出で保存するキー(役割)。LLMに見せる名前・説明・報告用の表示名はプロファイルで変えられる
ROLES = ["matrix", "analytes", "sample_preparation", "chromatography_column", "internal_standard", "lloq", "validation_type"]
# 題名に手法を示す語があれば優先して処理する(どのトピックでも使える既定)
GENERIC_TITLE = r"determination|quantif|validat|method|assay|simultaneous|detection|analysis of|measurement|development|approach|algorithm|pipeline|protocol|benchmark|comparison"

# 組み込みの例(ニコチンのLC-MS/MS)。トピック1と、LLMに新プロファイルを作らせる時の見本に使う
NICOTINE = {
    "schema_version": "1.1",
    "context": "an analytical (LC-MS/MS) method",
    "entity_regex": "nicotin|cotinin|anabasin|anatabin",
    "title_priority_regex": GENERIC_TITLE,
    "roles": {
        "matrix": {"name": "matrix", "label": "Sample type", "desc": "sample type analysed (e.g. urine, plasma, saliva, hair, e-liquid, wastewater)"},
        "analytes": {"name": "analytes", "label": "Analytes", "desc": "compounds quantified (e.g. nicotine, cotinine)"},
        "sample_preparation": {"name": "sample_preparation", "label": "Sample preparation", "desc": "how the sample was prepared/extracted before injection"},
        "chromatography_column": {"name": "chromatography_column", "label": "Column", "desc": "LC column used (type/brand/dimensions)"},
        "internal_standard": {"name": "internal_standard", "label": "Internal standard", "desc": "internal standard(s) used"},
        "lloq": {"name": "lloq", "label": "LLOQ", "desc": "lower limit of quantification (or detection) with its unit, as written"},
        "validation_type": {"name": "validation_type", "label": "Validation", "desc": "one of: none, internal, independent, experimental (internal = validation by the authors' own samples/guidelines)"},
    },
    "condition_classes": [
        ["urine", "urin"], ["plasma/serum/blood", "plasma|serum|whole blood|blood"], ["saliva/oral fluid", "saliva|oral fluid"],
        ["hair", "hair"], ["e-liquid/aerosol", "e-liquid|e-cig|vap|aerosol|liquid"], ["tobacco/smoke", "tobacco|cigarette|smoke"],
        ["wastewater", "wastewater|sewage|sewer"], ["tissue", "tissue|brain|lung|liver|placenta|cell|organ"],
        ["breast milk", "milk"], ["dried blood spot", "dried blood|dbs"]],
    "method_classes": [
        ["QuEChERS", "quechers|quick, easy, cheap"],
        ["supported liquid extraction", "supported liquid|\\bSLE\\b"],
        ["microextraction (SPME)", "microextraction|\\bSPME\\b"],
        ["solid-phase extraction", "solid.phase|\\bSPE\\b|cartridge|oasis|on-?line|column.switching|turbulent"],
        ["liquid-liquid extraction", "liquid.liquid|\\bLLE\\b|extracted with|extraction with|dichloromethane|ethyl acetate|\\bMTBE\\b|hexane|\\bether\\b"],
        ["protein precipitation", "precipitat|deproteini|znso4|zinc sulfate|acetonitrile (was )?added|centrifugation of .{0,20}with (acetonitrile|methanol)|diluted with .{0,25}(acetonitrile|methanol)|cold acetone"],
        ["dilute-and-shoot", "dilut(ion|ed)|direct(ly)? inject|minimal sample prep|simple (dilution|prep)"],
        ["solvent/ultrasound extraction", "sonicat|incubat|ultrasound|microwave|\\bMAE\\b|pressurized|\\bPLE\\b|soaked|one-step extraction|single-step extraction|extract"]],
    "detail_a_classes": [
        ["C18", "c\\(?18\\)?|\\bODS\\b|HSS T3|octadecyl"], ["HILIC", "hilic|hydrophilic interaction|\\bamide\\b"],
        ["biphenyl/phenyl/PFP", "biphenyl|phenyl|pfp|pentafluoro|\\bF5\\b"], ["C8", "\\bc8\\b"],
        ["porous graphitic carbon", "graphitic|hypercarb|\\bPGC\\b"], ["reversed-phase (type not stated)", "reverse.?d?.?phase|\\bRP\\b"]],
    "detail_a_generic": ["other", "reversed-phase (type not stated)"],
    "detail_b_specific_regex": "[a-z]+-?d\\s?\\d|13c|15n|\\([^)]*d\\d",
    "performance_lower_is_better": True,
    "gap_terms": {
        "urine": "urine", "plasma/serum/blood": "(plasma OR serum OR blood)", "saliva/oral fluid": "(saliva OR oral fluid)",
        "hair": "hair", "e-liquid/aerosol": "(e-liquid OR e-cigarette liquid)", "wastewater": "wastewater",
        "tissue": "(tissue OR brain)", "breast milk": "breast milk", "dried blood spot": "dried blood spot"},
    "gap_query_template": "nicotine AND (LC-MS/MS OR tandem mass spectrometry) AND {terms}",
    "entity_query_template": "\"{entity}\" AND (LC-MS/MS OR tandem mass spectrometry)",
    "keyword_hint": "related analytes (e.g. metabolites), matrices (urine, plasma, e-liquid, tobacco), and specific method names; each query must combine the analyte and the analytical method",
    "sanity": {"label": "isotope-labelled internal standard for urine/plasma", "field": "internal_standard",
               "regex": "d\\d|13c|15n|deuter|labell?ed|isotop", "conditions": ["urine", "plasma/serum/blood"]},
}


def validate(p):
    # プロファイルの形を確認する。問題があれば文字列のリストで返す(空なら正常)
    errs = []
    for k in ("entity_regex", "title_priority_regex", "detail_b_specific_regex"):
        try:
            re.compile(p.get(k) or ".")
        except re.error as e:
            errs.append(f"{k}: bad regex ({e})")
    for r in ROLES:
        if not isinstance(p.get("roles", {}).get(r), dict) or not all(x in p["roles"][r] for x in ("name", "label", "desc")):
            errs.append(f"roles.{r} needs name, label, desc")
    for k in ("condition_classes", "method_classes", "detail_a_classes"):
        for item in p.get(k) or []:
            if not (isinstance(item, list) and len(item) == 2):
                errs.append(f"{k}: each item must be [name, regex]")
                continue
            try:
                re.compile(item[1])
            except re.error as e:
                errs.append(f"{k}: bad regex for {item[0]} ({e})")
    if "{terms}" not in (p.get("gap_query_template") or ""):
        errs.append("gap_query_template must contain {terms}")
    if "{entity}" not in (p.get("entity_query_template") or ""):
        errs.append("entity_query_template must contain {entity}")
    if not isinstance(p.get("gap_terms"), dict):
        errs.append("gap_terms must be an object")
    return errs


def generic(topic_name):
    # LLM生成が失敗した時の最小プロファイル(分類規則なし。人が編集して使う)
    p = json.loads(json.dumps(NICOTINE))
    p.update({"context": "a method", "entity_regex": ".", "condition_classes": [], "method_classes": [], "detail_a_classes": [],
              "detail_a_generic": ["other"], "gap_terms": {}, "sanity": None,
              "gap_query_template": f"{topic_name} AND {{terms}}", "entity_query_template": f"\"{{entity}}\" AND {topic_name}",
              "keyword_hint": "synonyms, abbreviations, named methods and neighbouring-field terms"})
    return p


def finalize(p):
    # LLM生成後の整形: LLMに見せる項目名を表示名から作り直す(技術用語の旧名が残ると混乱するため)。
    # 信頼できない sanity は外し、ニコチン用の内標正規表現が残っていれば空にする
    used = set()
    for role in ROLES:
        r = p["roles"][role]
        if role != "validation_type" and r.get("label"):
            nm = re.sub(r"[^a-z0-9]+", "_", r["label"].lower()).strip("_") or role
            r["name"] = nm if nm not in used else f"{nm}_{role}"
        used.add(r["name"])
    p["sanity"] = None
    if p.get("detail_b_specific_regex") == NICOTINE["detail_b_specific_regex"]:
        p["detail_b_specific_regex"] = ""
    return p


def _ask(cfg, prompt, model):
    # Ollama にJSONで答えさせる(温度0・seed固定)
    r = httpx.post(f"{cfg['ollama_url']}/api/generate", timeout=900, json={
        "model": model, "prompt": prompt, "stream": False, "think": False, "format": "json",
        "options": {"temperature": 0, "seed": 0, "num_ctx": 8192, "num_predict": 3500}})
    r.raise_for_status()
    return json.loads(r.json()["response"])


def generate(topic_name, seeds):
    # 新トピックのプロファイルをLLMに作らせる(ニコチンの例を見本にして、形式を検証し、失敗したら誤りを伝えて再試行)
    cfg = load_config()
    model = cfg.get("profile_model") or cfg["models"]["llm"]
    example = json.dumps(NICOTINE, indent=1)
    base = (
        "You design a knowledge-extraction profile for a literature-mining system that finds the BEST METHOD per condition.\n"
        f"New topic: {topic_name}\nSeed queries: {seeds}\n\n"
        "Below is a COMPLETE example profile for another topic (nicotine LC-MS/MS). Produce a profile of exactly the same JSON structure for the NEW topic.\n"
        "Rules: keep the 7 role keys under roles (matrix, analytes, sample_preparation, chromatography_column, internal_standard, lloq, validation_type) but rewrite their name/label/desc to fit the new topic "
        "(role meanings: matrix = the condition/sample/organism/dataset the method is applied to; analytes = the target entities; sample_preparation = the main METHOD CLASS being compared; "
        "chromatography_column = a second technical detail (e.g. instrument, platform, algorithm, reagent); internal_standard = a third detail (e.g. control, reference, normalisation); "
        "lloq = the main performance metric with unit as written; validation_type must keep its desc about none/internal/independent/experimental). "
        "entity_regex: case-insensitive regex of word stems that any relevant paper must mention (broad, high recall). "
        "condition_classes/method_classes/detail_a_classes: ordered [name, regex] lists, first match wins, regex lower-case stems, most specific first. "
        "gap_terms: condition class name -> PubMed term. gap_query_template must contain {terms}; entity_query_template must contain {entity}. "
        "sanity: null if no obvious sanity check. Set performance_lower_is_better correctly for the metric. Keep schema_version \"1.1\". Output JSON only.\n\n"
        f"EXAMPLE PROFILE:\n{example}")
    prompt, last_err = base, []
    for _ in range(3):
        try:
            p = _ask(cfg, prompt, model)
            errs = validate(p)
        except (json.JSONDecodeError, httpx.HTTPError) as e:
            p, errs = None, [f"invalid JSON or request failure: {e}"]
        if not errs:
            p = finalize(p)
            p["schema_version"] = None  # 版はトピック作成後に t<ID>-1 として決める(トピック間で抽出結果が混ざらないように)
            return p, True
        last_err = errs
        prompt = base + "\n\nYour previous answer had these problems, fix them:\n- " + "\n- ".join(errs)
    print("profile generation failed, using generic profile:", last_err)
    return generic(topic_name), False


def generate_criteria(topic_name, seeds):
    # 採用/除外の基準文をLLMに下書きさせる(ニコチンの基準を書式の見本にする)
    cfg = load_config()
    example = Path("docs/criteria_nicotine_lcmsms.md").read_text() if Path("docs/criteria_nicotine_lcmsms.md").exists() else ""
    prompt = (f"Write screening criteria for a literature review on: {topic_name}\nSeed queries: {seeds}\n"
              "Use the same layout as the example: an INCLUDE paragraph, an EXCLUDE list, and a short UNSURE rule. Be specific and strict. "
              'Return JSON {"criteria": "<plain text>"}.\n\nEXAMPLE:\n' + example)
    try:
        return _ask(cfg, prompt, cfg.get("profile_model") or cfg["models"]["llm"])["criteria"]
    except Exception:
        return f"INCLUDE primary research papers that develop, validate, compare or apply methods for: {topic_name}.\nEXCLUDE papers that are not about this topic, and reviews, editorials or letters without original data.\nUse UNSURE only when the abstract is missing or ambiguous."


def load(conn, topic_id):
    # トピックのプロファイルを返す。無ければ、トピック1だけニコチンの組み込み版、他はエラー(別トピックに誤った規則を使わないため)
    r = conn.execute("SELECT profile FROM topics WHERE id=?", (topic_id,)).fetchone()
    if r and r["profile"]:
        return json.loads(r["profile"])
    if topic_id == 1:
        return NICOTINE  # 最初のトピック(ニコチン)は旧版で作られたので、組み込み版を使う
    raise ValueError(f"topic {topic_id} has no profile: run  ./run_local.sh create-topic --name <its name> --seeds <...>  to generate one")


def save(conn, topic_id, p):
    # DBに保存し、編集用に profiles/<id>.json にも書き出す
    p = dict(p)
    conn.execute("UPDATE topics SET profile=? WHERE id=?", (json.dumps(p, ensure_ascii=False), topic_id))
    conn.commit()
    PROFILE_DIR.mkdir(exist_ok=True)
    path = PROFILE_DIR / f"{topic_id}.json"
    path.write_text(json.dumps(p, indent=2, ensure_ascii=False))
    return path


def rx(p, key):
    # プロファイル中の正規表現をコンパイルして返す
    return re.compile(p[key], re.I)
