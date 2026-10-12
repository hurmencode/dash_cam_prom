#!/bin/bash
cd "$(dirname "$0")"

# Пути к Aravis, собранному из исходников
export GI_TYPELIB_PATH="/usr/local/lib/aarch64-linux-gnu/girepository-1.0:$GI_TYPELIB_PATH"
export LD_LIBRARY_PATH="/usr/local/lib/aarch64-linux-gnu:$LD_LIBRARY_PATH"

source .venv/bin/activate
python python_UI/ui.py