#!/bin/bash
# ローカル実行用: 初回は .venv を作って依存を入れ、その後は引数をそのまま app.cli に渡す
# 使い方: ./run_local.sh <command> [options]    例: ./run_local.sh screen --topic 1
#         ./run_local.sh serve                  (APIサーバを http://localhost:8000 で起動)
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  /opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
if [ "$1" = "serve" ]; then
  exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
fi
if [ "$1" = "notebook" ]; then
  exec .venv/bin/jupyter notebook method_finder.ipynb
fi
exec .venv/bin/python -m app.cli "$@"
