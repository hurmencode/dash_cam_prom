"""
Проверка ffmpeg: работают ли режимы записи, которые использует приложение.

    python3 tools/check_ffmpeg.py

Для каждого режима делает пробную запись нескольких крошечных кадров и
пишет OK / НЕ РАБОТАЕТ. Если какой-то режим не работает, приложение
автоматически пишет через OpenCV (см. логи).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
logging.basicConfig(level=logging.ERROR)
from backend.recorder import ffmpeg_path, ffmpeg_selftest  # noqa: E402

print("ffmpeg:", ffmpeg_path() or "НЕ НАЙДЕН")
modes = [
    ("copy_mjpeg", True,  "USB: JPEG-кадры камеры как есть"),
    ("mjpeg",      False, "GigE Mono8 -> MJPG"),
    ("mjpeg",      True,  "цветной кадр -> MJPG"),
    ("ffv1",       False, "GigE Mono8 -> FFV1 (lossless)"),
    ("ffv1",       True,  "цветной кадр -> FFV1 (lossless)"),
]
bad = 0
for kind, color, title in modes:
    ok, msg = ffmpeg_selftest(kind, color)
    bad += (not ok)
    print(f"  {'OK         ' if ok else 'НЕ РАБОТАЕТ'}  {title}"
          + ("" if ok else f"\n      {msg[:200]}"))
sys.exit(1 if bad else 0)
