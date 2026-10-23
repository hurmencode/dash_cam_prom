"""
Замер: тянет ли Jetson запись нужного разрешения и FPS выбранным кодеком.

  python3 tools/bench_encoder.py --mode mjpg --width 1936 --height 1464 \
        --fps 30 --seconds 20 --quality 95
  python3 tools/bench_encoder.py --mode ffv1 ...

Кадры синтетические (градиент + шум), реальный размер файла будет
другим, а вот загрузка CPU и потери кадров — показательны.
Во время замера в другом терминале смотрите `tegrastats` / `htop`.
Если «потеряно» > 0 — кодек/диск не успевают на этом режиме.
"""
import argparse
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backend.recorder import SegmentedRecorder  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mjpg", "ffv1"], default="mjpg")
    ap.add_argument("--width", type=int, default=1936)
    ap.add_argument("--height", type=int, default=1464)
    ap.add_argument("--fps", type=float, default=30)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--color", action="store_true")
    ap.add_argument("--dir", default=tempfile.gettempdir(),
                    help="куда писать (проверяйте на целевом диске!)")
    a = ap.parse_args()

    rng = np.random.default_rng(0)
    shape = (a.height, a.width, 3) if a.color else (a.height, a.width)
    grad = np.linspace(0, 255, a.width, dtype=np.uint8)
    pool = []
    for i in range(8):
        f = np.broadcast_to(grad if not a.color else grad[None, :, None],
                            shape).copy()
        f = f + rng.integers(0, 12, shape, dtype=np.uint8)
        pool.append(f)

    out = os.path.join(a.dir, "bench_encoder")
    rec = SegmentedRecorder(
        out, "bench", a.width, a.height, a.fps, a.color, source="raw",
        codec="ffv1" if a.mode == "ffv1" else "mjpeg",
        quality=a.quality, min_free_mb=500, segment_seconds=3600)
    rec.start()
    t0 = time.time()
    n = 0
    while time.time() - t0 < a.seconds:
        due = t0 + n / a.fps
        now = time.time()
        if now < due:
            time.sleep(due - now)
        rec.submit(pool[n % len(pool)])
        n += 1
    paths = rec.stop()
    dt = time.time() - t0
    size = sum(os.path.getsize(p) for p in paths) / 1e6
    print(f"режим={a.mode} {a.width}x{a.height} {'BGR' if a.color else 'Mono8'}"
          f" целевой FPS={a.fps}")
    print(f"кодек: {rec.codec_desc}")
    print(f"подано кадров: {n}, записано: {rec.frames_written}, "
          f"потеряно: {rec.frames_dropped}")
    print(f"размер: {size:.0f} MB за {dt:.0f} с "
          f"({size / dt * 3600 / 1000:.0f} GB/час, на синтетике)")
    for p in paths:
        os.remove(p)
        csv = os.path.splitext(p)[0] + ".csv"
        if os.path.exists(csv):
            os.remove(csv)


if __name__ == "__main__":
    main()
