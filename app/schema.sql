-- SPEC 第6章のデータモデル。すべて IF NOT EXISTS なので何度実行しても安全。

-- 研究トピック(スクリーニング基準 criteria もここに保存)
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT,
  criteria TEXT,
  profile TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 実行ログ(全ステージ・全LLM呼び出しのモデル名/版/プロンプト版を記録)
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL,
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  model TEXT,
  model_version TEXT,
  prompt_version TEXT,
  params_json TEXT,
  notes TEXT
);

-- 検索語(origin: llm / mesh / seed / snowball)
CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER REFERENCES topics(id),
  term TEXT NOT NULL,
  origin TEXT CHECK (origin IN ('llm','mesh','seed','snowball')),
  source TEXT,
  run_id INTEGER REFERENCES runs(id),
  hit_count INTEGER,
  status TEXT,
  precision REAL,
  novelty REAL,
  reason TEXT
);

-- 論文本体(primary_paper_id: 重複時に指す正本。ライセンスとURLは必須管理)
CREATE TABLE IF NOT EXISTS papers (
  id INTEGER PRIMARY KEY,
  doi TEXT, pmid TEXT, pmcid TEXT, arxiv_id TEXT,
  title TEXT, authors TEXT, year INTEGER, journal TEXT, abstract TEXT,
  source TEXT, url TEXT, licence TEXT,
  is_preprint INTEGER DEFAULT 0,
  is_retracted INTEGER DEFAULT 0,
  primary_paper_id INTEGER REFERENCES papers(id),
  status TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_pmid ON papers(pmid);

-- スクリーニング結果(reviewer は llm / human / rule)
CREATE TABLE IF NOT EXISTS screening (
  paper_id INTEGER REFERENCES papers(id),
  topic_id INTEGER REFERENCES topics(id),
  label TEXT CHECK (label IN ('include','exclude','unsure')),
  reason TEXT,
  reviewer TEXT CHECK (reviewer IN ('llm','human','rule')),
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (paper_id, topic_id)
);

-- 論文のセクション(解析品質つき)
CREATE TABLE IF NOT EXISTS sections (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER REFERENCES papers(id),
  name TEXT,
  text TEXT,
  parse_quality TEXT
);

-- 検索用チャンク(セクションをまたがない)
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER REFERENCES papers(id),
  section_id INTEGER REFERENCES sections(id),
  text TEXT,
  token_count INTEGER
);
-- キーワード検索用 FTS5(外部コンテンツ方式: chunks.text を参照)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='id'
);

-- 構造化抽出結果(JSON、検証状態つき)
CREATE TABLE IF NOT EXISTS extractions (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER REFERENCES papers(id),
  schema_version TEXT,
  json TEXT,
  status TEXT,
  run_id INTEGER REFERENCES runs(id)
);

-- 手法の分類体系とタグ
CREATE TABLE IF NOT EXISTS taxonomy (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER REFERENCES topics(id),
  name TEXT NOT NULL,
  parent_id INTEGER REFERENCES taxonomy(id),
  definition TEXT
);
CREATE TABLE IF NOT EXISTS paper_tags (
  paper_id INTEGER REFERENCES papers(id),
  taxonomy_id INTEGER REFERENCES taxonomy(id),
  confidence REAL,
  reviewer TEXT,
  PRIMARY KEY (paper_id, taxonomy_id)
);

-- どの検索(トピック)がどの論文を集めたか。トピックごとに論文を分けるための紐づけ
CREATE TABLE IF NOT EXISTS topic_papers (
  topic_id INTEGER REFERENCES topics(id),
  paper_id INTEGER REFERENCES papers(id),
  PRIMARY KEY (topic_id, paper_id)
);

-- 全文取得の試行記録(情報源ごと: pmc / europepmc)。status: ok / licence_not_usable / not_open_access / no_body など
CREATE TABLE IF NOT EXISTS fulltext (
  paper_id INTEGER REFERENCES papers(id),
  source TEXT,
  status TEXT,
  licence TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (paper_id, source)
);

-- チャンクの埋め込みベクトル(float32のバイト列)。FAISSインデックスはここから作り直せる(派生データ)
CREATE TABLE IF NOT EXISTS chunk_vectors (
  chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
  model TEXT,
  dim INTEGER,
  vec BLOB
);
