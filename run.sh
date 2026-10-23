#!/bin/bash
# Запуск Видеорегистратора 3000 (Jetson / Linux)
cd "$(dirname "$0")" || exit 1

# Aravis, собранный из исходников (если собирали в /usr/local).
# С пакетным gir1.2-aravis-0.8 эти пути не нужны.
for d in /usr/local/lib/aarch64-linux-gnu /usr/local/lib/x86_64-linux-gnu; do
    if [ -d "$d/girepository-1.0" ]; then
        export GI_TYPELIB_PATH="$d/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
        export LD_LIBRARY_PATH="$d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
done

# venv создаётся с --system-site-packages (см. README)
[ -f .venv/bin/activate ] && source .venv/bin/activate

exec python3 python_UI/ui.py "$@"
