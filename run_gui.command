#!/bin/zsh
# Double-clickable GUI launcher for macOS (or run from a terminal).
# Uses `conda run` to execute inside the mp-ternaries environment without
# requiring manual activation. Opens via a login shell, so MP_API_KEY set in
# ~/.zshrc is inherited.

cd "$(dirname "$0")"

if ! command -v conda >/dev/null 2>&1; then
  echo "conda not found on PATH."
  echo "Install conda and run:  conda env create -f environment.yml"
  read -k 1 -s "?Press any key to close."
  exit 1
fi

if ! conda env list | grep -qE '^\s*mp-ternaries\s'; then
  echo "Environment 'mp-ternaries' not found. Creating it now (one time)..."
  conda env create -f environment.yml || {
    read -k 1 -s "?Env creation failed. Press any key to close."; exit 1; }
fi

exec conda run --no-capture-output -n mp-ternaries python gui.py
