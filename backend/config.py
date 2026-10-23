"""
Настройки приложения.

Значения по умолчанию можно переопределить файлом
~/dash_cam_prom/config.json (создаётся автоматически при первом
сохранении настроек) или переменными окружения DASHCAM_<ИМЯ>,
например DASHCAM_ENCODER=nvjpeg.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict, fields
from typing import Optional

log = logging.getLogger("dashcam.config")

APP_DIR = os.path.join(os.path.expanduser("~"), "dash_cam_prom")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")


@dataclass
class Settings:
    # --- запись ---
    min_free_mb: int = 5000           # ниже порога запись останавливается
    segment_seconds: int = 3600       # длина одного файла
    # auto  — USB, отдающий MJPG, пишется как есть (без пережатия);
    #         остальное — MJPG через OpenCV (CPU)
    # mjpg  — всегда MJPG через OpenCV (для USB это декодирование+пережатие)
    # ffv1  — lossless через ffmpeg (много места и CPU)
    encoder: str = "auto"
    jpeg_quality: int = 95            # для mjpg; для обучения лучше 95+
    container: str = "mkv"            # mkv устойчивее к обрыву питания, чем avi
    queue_frames: int = 64            # буфер между захватом и кодированием
    warmup_seconds: float = 1.0       # измерение реального FPS перед записью
    write_timestamps: bool = True     # CSV с номером и временем каждого кадра

    # --- превью ---
    preview_fps: int = 15             # ограничение частоты обновления превью

    # --- GigE ---
    gige_buffers: int = 32
    gige_target_fps: Optional[float] = None

    # --- USB ---
    usb_width: Optional[int] = None
    usb_height: Optional[int] = None
    usb_fps: Optional[int] = None
    usb_use_mjpg: bool = True         # запросить у UVC-камеры MJPG (больше FPS)
    usb_passthrough: bool = True      # брать из камеры сырые JPEG-пакеты (без декодирования)

    # --- запомненные пути ---
    last_record_dir: str = ""
    last_snapshot_dir: str = ""

    def save(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            log.warning("Не удалось сохранить настройки: %s", e)


def _coerce(value, default):
    """Приводит значение из JSON/env к типу значения по умолчанию."""
    if default is None:
        return value
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return type(default)(value)


def load_settings() -> Settings:
    s = Settings()
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Не удалось прочитать %s: %s", CONFIG_PATH, e)

    for fld in fields(Settings):
        raw = None
        if fld.name in data:
            raw = data[fld.name]
        env = os.environ.get("DASHCAM_" + fld.name.upper())
        if env is not None:
            raw = env
        if raw is None:
            continue
        default = getattr(s, fld.name)
        try:
            # поля Optional[...] с default None: берём число как есть
            if default is None:
                val = None if str(raw).lower() in ("", "none", "null") \
                    else float(raw) if "." in str(raw) else int(raw)
            else:
                val = _coerce(raw, default)
            setattr(s, fld.name, val)
        except Exception as e:
            log.warning("Некорректное значение %s=%r: %s", fld.name, raw, e)

    s.jpeg_quality = min(100, max(1, s.jpeg_quality))
    s.segment_seconds = max(60, s.segment_seconds)
    s.queue_frames = max(8, s.queue_frames)
    s.preview_fps = max(1, s.preview_fps)
    s.encoder = s.encoder.lower() if s.encoder.lower() in (
        "auto", "mjpg", "ffv1") else "auto"
    s.container = s.container.lower() if s.container.lower() in (
        "avi", "mkv") else "mkv"
    return s
