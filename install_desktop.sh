#!/bin/bash
# Создаёт ярлык на рабочем столе с правильными путями (без правки файла вручную)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$DESK"
sed "s#@APP_DIR@#$APP_DIR#g" "$APP_DIR/dash_cam.desktop.in" > "$DESK/dash_cam.desktop"
chmod +x "$DESK/dash_cam.desktop"
gio set "$DESK/dash_cam.desktop" metadata::trusted true 2>/dev/null
echo "Ярлык создан: $DESK/dash_cam.desktop"
