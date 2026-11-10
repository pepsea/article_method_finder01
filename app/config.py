"""モデル名などの設定を config.yaml から読む(差し替えを1か所で行うため)。"""
import os
from pathlib import Path

import yaml

# 設定ファイルの場所。環境変数 CONFIG_PATH で変更できる
CONFIG_PATH = os.environ.get("CONFIG_PATH", str(Path(__file__).parent.parent / "config.yaml"))


def load_config():
    # YAML を読んで辞書で返す。Ollama の接続先は環境変数 OLLAMA_URL があれば上書きする
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    if os.environ.get("OLLAMA_URL"):
        cfg["ollama_url"] = os.environ["OLLAMA_URL"]
    return cfg
