#!/usr/bin/env python3
"""
Оценка USB-камеры для набора данных: какие режимы поддерживает и что
РЕАЛЬНО отдаёт на Jetson (а не то, что написано в паспорте).

    python3 tools/check_usb_camera.py                # /dev/video0, 1920x1200, 10 с
    python3 tools/check_usb_camera.py 1 1280 720 20  # устройство, ширина, высота, секунд

Меряет в режиме MJPG без декодирования (так приложение пишет датасет):
реальный FPS, стабильность интервалов между кадрами, размер JPEG,
долю битых кадров, загрузку CPU процессом. Заодно сохраняет один кадр
check_frame.jpg — по нему смотрят резкость, шум, смаз и т.п.
"""
import os
import shutil
import statistics
import subprocess
import sys
import time

import cv2
import numpy as np


def main():
    dev = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1920
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
    secs = float(sys.argv[4]) if len(sys.argv) > 4 else 10.0

    if shutil.which("v4l2-ctl"):
        print(subprocess.run(
            ["v4l2-ctl", "-d", f"/dev/video{dev}", "--list-formats-ext"],
            capture_output=True, text=True).stdout)

    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
    if not cap.isOpened():
        sys.exit("Не удалось открыть камеру")
    rw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    rh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"Запрошено {w}x{h}, получено {rw}x{rh}, "
          f"заявленный FPS {cap.get(cv2.CAP_PROP_FPS):.1f}")

    times, sizes, bad = [], [], 0
    last = None
    cpu0, t0 = time.process_time(), time.time()
    while time.time() - t0 < secs:
        ok, buf = cap.read()
        now = time.time()
        if not ok or buf is None:
            continue
        flat = buf.reshape(-1)
        if not (flat.size > 4 and flat[0] == 0xFF and flat[1] == 0xD8
                and flat[-2] == 0xFF and flat[-1] == 0xD9):
            bad += 1
            continue
        times.append(now)
        sizes.append(flat.size)
        last = buf.copy()
    wall, cpu = time.time() - t0, time.process_time() - cpu0
    cap.release()

    if len(times) < 3:
        sys.exit("Кадры не получены (камера не отдаёт MJPG в этом режиме?)")

    dt = np.diff(times)
    fps = (len(times) - 1) / (times[-1] - times[0])
    exp = statistics.median(dt)
    gaps = int((dt > 1.5 * exp).sum())
    print(f"\nКадров: {len(times)}, битых JPEG: {bad}")
    print(f"Реальный FPS: {fps:.2f}")
    print(f"Интервал между кадрами: медиана {exp*1000:.1f} мс, "
          f"макс {dt.max()*1000:.1f} мс, пропусков (>1.5x): {gaps}")
    mb_s = np.mean(sizes) * fps / 1e6
    print(f"Размер JPEG: средний {np.mean(sizes)/1024:.0f} КБ "
          f"-> поток {mb_s:.1f} МБ/с, {mb_s*3.6:.0f} ГБ/час")
    print(f"CPU процесса: {100*cpu/wall:.0f}% одного ядра")

    img = cv2.imdecode(last.reshape(-1), cv2.IMREAD_COLOR)
    cv2.imwrite("check_frame.jpg", img)
    print("Кадр сохранён: check_frame.jpg")


if __name__ == "__main__":
    main()
