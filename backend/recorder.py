"""
Запись видео для набора данных (обучение нейросетей).

У Jetson Orin Nano нет аппаратного видеокодера NVENC, поэтому всё, что
кодируется, кодируется на CPU. Отсюда три режима записи кадра:

* mjpeg_raw — USB-камера уже отдаёт готовые JPEG-кадры (MJPG). Пишем их в
  контейнер КАК ЕСТЬ через `ffmpeg -c copy`: нулевая нагрузка на CPU и
  никакой потери качества от повторного сжатия.
* ffmpeg    — «сырые» кадры (Mono8 с GigE, BGR) кодирует отдельный
  процесс ffmpeg (не занимает GIL, использует несколько ядер):
    - mjpeg — с потерями, качество задаётся (по умолчанию 95);
    - ffv1  — без потерь (для Mono8 вдвое-втрое меньше raw).
* cv2       — запасной вариант без ffmpeg: cv2.VideoWriter MJPG.

Рядом с каждым сегментом пишется <имя>.csv с меткой времени прихода
каждого кадра — по ней видно потерянные кадры и можно синхронизировать
камеры между собой.

SegmentedRecorder принимает кадры через неблокирующий submit() и кодирует
их в отдельном потоке: медленный диск или кодек не тормозит приём кадров
с камеры, а VideoWriter создаётся и закрывается ровно одним потоком.
"""

import logging
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger("dashcam.recorder")


_ffmpeg_override: Optional[str] = None


def set_ffmpeg_path(path: Optional[str]):
    """Явный путь к ffmpeg (настройка ffmpeg_path / DASHCAM_FFMPEG)."""
    global _ffmpeg_override
    _ffmpeg_override = path or None
    _selftest_cache.clear()


def ffmpeg_path() -> Optional[str]:
    return _ffmpeg_override or shutil.which("ffmpeg")


def free_mb(path: str) -> float:
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 * 1024)
    except Exception:
        return float("inf")


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-_.]+", "_", name).strip("_") or "camera"


def is_valid_jpeg(buf: np.ndarray) -> bool:
    """Дёшево отсекает обрезанные UVC-кадры: SOI в начале и EOI в конце."""
    if buf.size < 4:
        return False
    flat = buf.reshape(-1)
    return (flat[0] == 0xFF and flat[1] == 0xD8
            and flat[-2] == 0xFF and flat[-1] == 0xD9)


def _jpeg_qv(quality: int) -> int:
    """Шкала 1..100 -> -q:v ffmpeg (2 лучшее ... 31 худшее)."""
    return int(max(2, min(31, round(2 + (100 - quality) * 0.29))))


# ---------------------------------------------------------------- писатели
class FfmpegWriter:
    """
    Пишет кадры в stdin процесса ffmpeg.
    kind: 'copy_mjpeg' | 'mjpeg' | 'ffv1'
    Для copy_mjpeg в write() передаётся массив с байтами JPEG-кадра.

    Рассчитано на урезанные сборки ffmpeg (Jetson: --disable-everything,
    --disable-swscale, протоколы только file,pipe), поэтому:
    * вход задаётся как pipe:0 (в ffmpeg 8 «-» превращается в fd:, а
      протокола fd в такой сборке нет);
    * для копирования JPEG используется демультиплексор mjpeg, а не
      image2pipe;
    * ffmpeg НИЧЕГО не конвертирует: формат пикселей приводится к тому,
      что кодек принимает напрямую, здесь, в Python (иначе нужен swscale):
        mjpeg: Mono8 -> yuvj420p (серый + нейтральный цвет),
               BGR -> yuvj420p (cv2, полный диапазон)
        ffv1:  Mono8 -> gray, BGR -> bgr0 (с пустым альфа-каналом)
    """

    def __init__(self, path: str, fps: float, size: Tuple[int, int],
                 is_color: bool, kind: str, quality: int = 95,
                 threads: int = 0, quiet: bool = False):
        self.quiet = quiet
        self.returncode = None
        self._conv = None
        w, h = size
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
               "-y"]
        if kind == "copy_mjpeg":
            cmd += ["-f", "mjpeg", "-framerate", f"{fps:.3f}",
                    "-i", "pipe:0", "-c:v", "copy"]
        elif kind == "ffv1":
            if is_color:
                in_pix = "bgr0"
                self._conv = lambda f: cv2.cvtColor(f, cv2.COLOR_BGR2BGRA)
            else:
                in_pix = "gray"
            cmd += ["-f", "rawvideo", "-pix_fmt", in_pix,
                    "-s", f"{w}x{h}", "-framerate", f"{fps:.3f}",
                    "-i", "pipe:0",
                    "-c:v", "ffv1", "-level", "3", "-coder", "1",
                    "-context", "1", "-g", "1", "-slices", "16",
                    "-slicecrc", "1"]
        else:  # mjpeg
            ew, eh = w & ~1, h & ~1          # yuv420: чётные размеры
            if is_color:
                self._conv = lambda f: cv2.cvtColor(
                    np.ascontiguousarray(f[:eh, :ew]),
                    cv2.COLOR_BGR2YUV_I420)
            else:
                buf = np.full((eh * 3 // 2, ew), 128, np.uint8)

                def conv(f, buf=buf, eh=eh, ew=ew):
                    buf[:eh] = f[:eh, :ew]   # плоскость Y; U/V = 128
                    return buf
                self._conv = conv
            cmd += ["-f", "rawvideo", "-pix_fmt", "yuvj420p",
                    "-s", f"{ew}x{eh}", "-framerate", f"{fps:.3f}",
                    "-i", "pipe:0",
                    "-c:v", "mjpeg", "-q:v", str(_jpeg_qv(quality))]
        if threads and kind != "copy_mjpeg":
            cmd += ["-threads", str(threads)]
        cmd += [path]

        self._err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=self._err, bufsize=0)
        self.path = path

    def write(self, frame: np.ndarray):
        if self.proc.poll() is not None:
            raise RuntimeError("ffmpeg завершился: " + self.error_tail())
        if self._conv is not None:
            frame = self._conv(frame)
        data = frame if frame.flags["C_CONTIGUOUS"] \
            else np.ascontiguousarray(frame)
        self.proc.stdin.write(memoryview(data).cast("B"))

    def error_tail(self) -> str:
        try:
            self._err.seek(0)
            return self._err.read()[-500:].decode("utf-8", "replace").strip()
        except Exception:
            return ""

    def isOpened(self) -> bool:
        return self.proc.poll() is None

    def release(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            log.error("ffmpeg не закрылся за 60 с — убит: %s", self.path)
        rc = self.returncode = self.proc.returncode
        if rc not in (0, None) and not self.quiet:
            log.error("ffmpeg завершился с кодом %s: %s", rc,
                      self.error_tail())
        try:
            self._err.close()
        except Exception:
            pass


class _DecodingWriter:
    """JPEG-пакет -> BGR -> inner (принудительное перекодирование)."""

    def __init__(self, inner):
        self.inner = inner

    def write(self, buf):
        img = cv2.imdecode(buf.reshape(-1), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("не удалось декодировать JPEG")
        self.inner.write(img)

    def release(self):
        self.inner.release()


class _Cv2Writer:
    """Запасной вариант без ffmpeg: cv2.VideoWriter MJPG.
    decode=True — на вход приходят JPEG-байты, их надо раскодировать."""

    def __init__(self, path, fps, size, is_color, quality, container,
                 decode=False):
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        api = cv2.CAP_FFMPEG if container == "mkv" else cv2.CAP_ANY
        self._w = cv2.VideoWriter(path, api, fourcc, fps, size,
                                  True if decode else is_color)
        if not self._w.isOpened():
            self._w.release()
            raise RuntimeError(f"Не удалось открыть VideoWriter: {path}")
        prop = getattr(cv2, "VIDEOWRITER_PROP_QUALITY", None)
        if prop is not None:
            try:
                self._w.set(prop, float(quality))
            except Exception:
                pass
        self._decode = decode

    def write(self, frame):
        if self._decode:
            frame = cv2.imdecode(frame.reshape(-1), cv2.IMREAD_COLOR)
            if frame is None:
                return
        self._w.write(frame)

    def release(self):
        self._w.release()


_selftest_cache: dict = {}


def ffmpeg_selftest(kind: str, is_color: bool = False) -> Tuple[bool, str]:
    """
    Пробный запуск ffmpeg с ТЕМИ ЖЕ ключами, что и при записи (вход из
    stdin, нужный кодек, контейнер mkv) на нескольких крошечных кадрах.
    Нужен, потому что часть сборок ffmpeg урезана: нет `image2pipe`,
    `rawvideo`, протокола pipe/fd и т.п. — тогда он падает при старте.
    Результат кэшируется. Возвращает (ok, текст_ошибки).
    """
    key = (ffmpeg_path(), kind, bool(is_color))
    if key in _selftest_cache:
        return _selftest_cache[key]

    ok, msg = False, ""
    if ffmpeg_path() is None:
        msg = "ffmpeg не найден в PATH"
    else:
        tmpdir = tempfile.mkdtemp(prefix="dashcam_ffcheck_")
        path = os.path.join(tmpdir, "check.mkv")
        w = None
        try:
            w, h = 64, 48
            w_ = FfmpegWriter(path, 30.0, (w, h), is_color, kind,
                              quiet=True)
            w = w_
            if kind == "copy_mjpeg":
                img = np.full((h, 64, 3), 127, np.uint8)
                frame = cv2.imencode(".jpg", img)[1].reshape(1, -1)
            else:
                shape = (h, 64, 3) if is_color else (h, 64)
                frame = np.full(shape, 127, np.uint8)
            for _ in range(3):
                w_.write(frame)
            w_.release()
            w = None
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if w_.returncode == 0 and size > 0:
                ok = True
            else:
                msg = w_.error_tail() or f"код возврата {w_.returncode}"
        except Exception as e:
            msg = (w.error_tail() if w is not None else "") or str(e)
        finally:
            if w is not None:
                try:
                    w.release()
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    _selftest_cache[key] = (ok, msg)
    if ok:
        log.info("ffmpeg (%s) проверен: режим %s работает", ffmpeg_path(),
                 kind)
    else:
        log.warning(
            "ffmpeg (%s) НЕ работает в режиме %s: %s\n"
            "  Скорее всего это урезанная сборка. Проверьте:\n"
            "    which -a ffmpeg; ffmpeg -version | head -2\n"
            "    ffmpeg -formats | grep -E 'image2pipe|rawvideo'\n"
            "    ffmpeg -protocols | grep -E 'pipe|fd'\n"
            "  Обычно помогает: sudo apt install --reinstall ffmpeg\n"
            "  или укажите другой бинарник: DASHCAM_FFMPEG_PATH=/путь/к/ffmpeg",
            ffmpeg_path(), kind, msg.replace("\n", " | ")[:300])
    return ok, msg


def open_writer(path: str, fps: float, size: Tuple[int, int],
                is_color: bool, source: str, codec: str,
                quality: int, container: str, reencode: bool = False):
    """
    source: 'mjpeg_raw' (готовые JPEG-кадры) | 'raw' (ndarray кадра)
    reencode: для 'mjpeg_raw' — не копировать JPEG, а декодировать и
              закодировать заново выбранным codec
    codec:  'mjpeg' | 'ffv1'        (для source='raw')
    Возвращает (writer, описание). Бросает RuntimeError при неудаче.
    ffmpeg используется, только если пробный запуск прошёл успешно,
    иначе — запасной режим через OpenCV (с понятным предупреждением).
    """
    fps = max(1.0, float(fps))

    if source == "mjpeg_raw":
        if not reencode:
            if ffmpeg_selftest("copy_mjpeg")[0]:
                return (FfmpegWriter(path, fps, size, True, "copy_mjpeg"),
                        "MJPG (без перекодирования)")
            log.warning("Запись USB «как есть» недоступна — JPEG будут "
                        "декодироваться и сжиматься заново (нагрузка на CPU "
                        "и потеря качества). Исправьте ffmpeg.")
        else:
            kind = "ffv1" if codec == "ffv1" else "mjpeg"
            if ffmpeg_selftest(kind, True)[0]:
                inner = FfmpegWriter(path, fps, size, True, kind, quality)
                return (_DecodingWriter(inner),
                        ("FFV1 (lossless, из декодированного JPEG)"
                         if kind == "ffv1"
                         else f"MJPG q{quality} (перекодирование)"))
        return (_Cv2Writer(path, fps, size, True, quality, container,
                           decode=True), "MJPG (cv2, перекодирование)")

    kind = "ffv1" if codec == "ffv1" else "mjpeg"
    if ffmpeg_selftest(kind, is_color)[0]:
        desc = ("FFV1 (lossless)" if kind == "ffv1"
                else f"MJPG q{quality} (ffmpeg)")
        return FfmpegWriter(path, fps, size, is_color, kind, quality), desc

    if codec == "ffv1":
        log.warning("FFV1 недоступен без рабочего ffmpeg — пишу MJPG через "
                    "OpenCV (с потерями!)")
    return (_Cv2Writer(path, fps, size, is_color, quality, container),
            f"MJPG q{quality} (cv2)")


# ------------------------------------------------------------- рекордер
class SegmentedRecorder:
    """Запись одной камеры в файлы-сегменты в отдельном потоке."""

    def __init__(self, output_dir: str, camera_name: str,
                 width: int, height: int, fps: float, is_color: bool,
                 *, source: str = "raw", codec: str = "mjpeg",
                 min_free_mb: int = 5000, segment_seconds: int = 3600,
                 quality: int = 95, container: str = "mkv",
                 queue_frames: int = 64, write_csv: bool = True,
                 reencode: bool = False,
                 on_disk_full: Optional[Callable[[float], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None):
        self.output_dir = output_dir
        self.camera_name = safe_filename(camera_name)
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1.0, float(fps))
        self.is_color = bool(is_color)
        self.source = source
        self.codec = codec
        self.min_free_mb = int(min_free_mb)
        self.segment_seconds = max(60, int(segment_seconds))
        self.quality = int(quality)
        self.container = container
        self.write_csv = write_csv
        self.reencode = reencode
        self.on_disk_full = on_disk_full
        self.on_error = on_error

        self._q: "queue.Queue" = queue.Queue(
            maxsize=max(8, int(queue_frames)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Пишет только поток записи, остальные читают.
        self.codec_desc: Optional[str] = None
        self.segment_index = 0
        self.segment_paths: List[str] = []
        self.frames_written = 0
        self.frames_dropped = 0     # очередь переполнена
        self.frames_corrupt = 0     # битые JPEG от камеры
        self.failed = False
        self._segment_start = 0.0
        self._segment_frame = 0
        self._offered = 0           # сколько кадров предложили submit()
        self._writer = None
        self._csv = None
        self._started_at = 0.0

    # ---------- публичное ----------
    def start(self) -> bool:
        os.makedirs(self.output_dir, exist_ok=True)
        free = free_mb(self.output_dir)
        if free < self.min_free_mb:
            raise RuntimeError(
                f"Мало места на диске: {free:.0f} MB "
                f"(нужно минимум {self.min_free_mb} MB)")
        self._open_segment()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name=f"rec-{self.camera_name}", daemon=True)
        self._thread.start()
        return True

    def submit(self, frame: np.ndarray, ts: Optional[float] = None) -> bool:
        """
        Неблокирующая постановка кадра. ts — время прихода (unix).
        Каждому предложенному кадру присваивается номер camera_seq; если
        кадр отброшен (очередь полна / битый JPEG), в CSV будет разрыв
        в нумерации.
        """
        seq = self._offered
        self._offered += 1
        if self._stop.is_set() or self.failed:
            return False
        if self.source == "mjpeg_raw":
            if not is_valid_jpeg(frame):
                self.frames_corrupt += 1
                return False
        elif frame.shape[0] != self.height or frame.shape[1] != self.width:
            self.frames_dropped += 1
            return False
        try:
            self._q.put_nowait(
                (frame, ts if ts is not None else time.time(), seq))
            return True
        except queue.Full:
            self.frames_dropped += 1
            return False

    def stop(self, timeout: float = 60.0) -> List[str]:
        """Дописывает очередь, закрывает файл, возвращает список сегментов."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                log.error("[%s] поток записи не завершился за %.0f с",
                          self.camera_name, timeout)
        self._thread = None
        return list(self.segment_paths)

    @property
    def queue_size(self) -> int:
        return self._q.qsize()

    @property
    def segment_remaining(self) -> float:
        if not self._segment_start:
            return 0.0
        return max(0.0, self.segment_seconds
                   - (time.time() - self._segment_start))

    # ---------- внутреннее ----------
    def _segment_path(self) -> str:
        return os.path.join(
            self.output_dir,
            f"{self.camera_name}_{self._timestamp}"
            f"_part{self.segment_index:03d}.{self.container}")

    def _open_segment(self):
        self.segment_index += 1
        path = self._segment_path()
        writer, desc = open_writer(
            path, self.fps, (self.width, self.height), self.is_color,
            self.source, self.codec, self.quality, self.container,
            self.reencode)
        self._writer = writer
        self.codec_desc = desc
        self._segment_start = time.time()
        self._segment_frame = 0
        self.segment_paths.append(path)
        if self.write_csv:
            try:
                self._csv = open(os.path.splitext(path)[0] + ".csv", "w",
                                 buffering=1)
                self._csv.write("frame_index,camera_seq,unix_time\n")
            except Exception as e:
                log.warning("CSV с метками времени не создан: %s", e)
                self._csv = None
        log.info("[%s] сегмент %d открыт: %s, %dx%d, %.2f fps -> %s",
                 self.camera_name, self.segment_index, desc,
                 self.width, self.height, self.fps, path)

    def _close_segment(self):
        w, self._writer = self._writer, None
        csv, self._csv = self._csv, None
        if csv is not None:
            try:
                csv.close()
            except Exception:
                pass
        if w is None:
            return
        try:
            w.release()
        except Exception:
            log.exception("[%s] ошибка закрытия сегмента", self.camera_name)
        path = self.segment_paths[-1] if self.segment_paths else None
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024) if path else 0
        except OSError:
            size_mb = 0
        log.info("[%s] сегмент %d закрыт: %d кадров, %.1f MB",
                 self.camera_name, self.segment_index,
                 self._segment_frame, size_mb)

    def _fail(self, message: str, notify: bool = True):
        self.failed = True
        log.error("[%s] %s", self.camera_name, message)
        if notify and self.on_error:
            try:
                self.on_error(message)
            except Exception:
                log.exception("on_error")

    def _run(self):
        last_disk_check = 0.0
        write_errors = 0
        try:
            while True:
                try:
                    frame, ts, seq = self._q.get(timeout=0.25)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue

                now = time.time()

                if now - last_disk_check >= 1.0:
                    last_disk_check = now
                    free = free_mb(self.output_dir)
                    if free < self.min_free_mb:
                        self._fail(f"Мало места на диске "
                                   f"({free:.0f} MB < {self.min_free_mb} MB), "
                                   f"запись остановлена", notify=False)
                        if self.on_disk_full:
                            try:
                                self.on_disk_full(free)
                            except Exception:
                                log.exception("on_disk_full")
                        break

                if now - self._segment_start >= self.segment_seconds:
                    self._close_segment()
                    try:
                        self._open_segment()
                    except Exception as e:
                        self._fail(f"Не удалось открыть новый сегмент: {e}")
                        break

                try:
                    self._writer.write(frame)
                    if self._csv is not None:
                        self._csv.write(
                            f"{self._segment_frame},{seq},{ts:.6f}\n")
                    self._segment_frame += 1
                    self.frames_written += 1
                    write_errors = 0
                except Exception as e:
                    write_errors += 1
                    if write_errors >= 5:
                        self._fail(f"Ошибки записи кадров: {e}")
                        break
        except Exception as e:
            self._fail(f"Сбой потока записи: {e}")
            log.exception("поток записи")
        finally:
            self._stop.set()
            self._close_segment()
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass
