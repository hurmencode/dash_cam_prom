"""
Работа с камерами: GigE/USB3 Vision через Aravis и UVC через OpenCV.

Правило для вызывающего кода: release() нельзя вызывать, пока другой
поток сидит в get_frame() — это use-after-free в C-библиотеке.
Сначала остановите поток захвата и дождитесь его завершения (join).
"""

import contextlib
import gc
import logging
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import cv2
import numpy as np

from .config import Settings

log = logging.getLogger("dashcam.camera")

# ---------- Aravis (необязателен: без него работают только USB-камеры) ----------
# JetPack 6 = Ubuntu 22.04, где в apt лежит Aravis 0.8 (gir1.2-aravis-0.8).
# Aravis 0.10 нужно собирать из исходников — поддерживаем обе версии.
Aravis = None
ARAVIS_VERSION = None
try:
    import gi
    for _ver in ("0.10", "0.8"):
        try:
            gi.require_version("Aravis", _ver)
            from gi.repository import Aravis  # noqa: F811
            ARAVIS_VERSION = _ver
            break
        except (ValueError, ImportError):
            Aravis = None
except ImportError:
    pass

if Aravis is None:
    log.warning("Aravis (gi) не найден — GigE-камеры недоступны, "
                "работают только USB")
else:
    log.info("Aravis %s", ARAVIS_VERSION)


# ============ БАЗОВЫЙ ИНТЕРФЕЙС ============
class CameraInterface(ABC):
    # 'gray'      — ndarray HxW (Mono8)
    # 'bgr'       — ndarray HxWx3
    # 'mjpeg_raw' — ndarray 1xN с готовым JPEG-кадром (не декодирован)
    frame_kind = "gray"

    def to_image(self, frame: np.ndarray) -> Optional[np.ndarray]:
        """Кадр в виде изображения (для превью/снимка)."""
        return frame

    @abstractmethod
    def get_frame(self, timeout_ms: int = 200) -> Optional[np.ndarray]:
        """Следующий кадр или None (таймаут / нет кадра)."""

    @abstractmethod
    def release(self):
        pass

    @abstractmethod
    def get_info(self) -> Dict:
        pass

    def get_stats(self) -> Dict:
        return {}


# ============ V4L2 INFO HELPERS ============
def _v4l2_info(device_id: int) -> dict:
    """model / driver / bus / serial для /dev/videoN через v4l2-ctl и /sys."""
    result = {"model": "N/A", "serial": "N/A", "driver": "N/A", "bus": "N/A"}
    try:
        out = subprocess.run(
            ["v4l2-ctl", "-d", f"/dev/video{device_id}", "--info"],
            capture_output=True, text=True, timeout=2.0)
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                key, sep, value = line.strip().partition(":")
                if not sep:
                    continue
                key = key.strip().lower()
                value = value.strip()
                if key == "card type":
                    result["model"] = value
                elif key == "bus info":
                    result["bus"] = value
                elif key == "driver name":
                    result["driver"] = value
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        log.debug("v4l2-ctl", exc_info=True)

    result["serial"] = _usb_serial_from_sys(device_id)
    return result


def _usb_serial_from_sys(device_id: int) -> str:
    try:
        base = f"/sys/class/video4linux/video{device_id}/device"
        if not os.path.exists(base):
            return "N/A"
        real = os.path.realpath(base)
        for _ in range(6):
            serial_path = os.path.join(real, "serial")
            if os.path.isfile(serial_path):
                with open(serial_path, "r") as f:
                    val = f.read().strip()
                if val:
                    return val
            parent = os.path.dirname(real)
            if parent == real:
                break
            real = parent
    except Exception:
        pass
    return "N/A"


def _open_v4l2(device_id: int):
    api = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_V4L2
    return cv2.VideoCapture(device_id, api)


# ============ USB (UVC) ============
class UsbCameraManager(CameraInterface):
    """USB-камера через OpenCV. Кадры — BGR (3 канала)."""

    def __init__(self, device_id: int = 0, settings: Optional[Settings] = None):
        s = settings or Settings()
        self.device_id = device_id
        self._lock = threading.Lock()
        self.cap = _open_v4l2(device_id)
        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Не удалось открыть USB-камеру {device_id}")

        # Порядок важен: сначала формат, потом размер и FPS.
        if s.usb_use_mjpg:
            self.cap.set(cv2.CAP_PROP_FOURCC,
                         cv2.VideoWriter_fourcc(*"MJPG"))
        if s.usb_width:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, s.usb_width)
        if s.usb_height:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, s.usb_height)
        if s.usb_fps:
            self.cap.set(cv2.CAP_PROP_FPS, s.usb_fps)
        # Не копить старые кадры в буфере драйвера
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Камера уже отдаёт MJPG: забираем готовые JPEG-пакеты без
        # декодирования. Для записи датасета это ноль нагрузки на CPU и
        # ноль потерь качества; декодируются только кадры для превью.
        self.frame_kind = "bgr"
        if s.usb_use_mjpg and s.usb_passthrough:
            self._try_enable_passthrough()

        # Сырые JPEG-пакеты: камера уже сжала кадр в MJPG, незачем
        # декодировать и пережимать (CPU + потеря качества).
        self.raw_jpeg = False
        if s.usb_use_mjpg and s.usb_passthrough:
            self.raw_jpeg = self._enable_raw_jpeg()

        real_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        real_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        real_fps = self.cap.get(cv2.CAP_PROP_FPS)

        info = (_v4l2_info(device_id) if os.name != "nt" else
                {"model": "N/A", "serial": "N/A", "driver": "N/A",
                 "bus": "N/A"})
        model = (info["model"] if info["model"] != "N/A"
                 else f"USB Camera {device_id}")

        self._info = {
            "name": model, "serial": info["serial"], "ip": "N/A",
            "status": "Connected", "width": real_w, "height": real_h,
            "fps": real_fps, "is_color": True,
            "raw_mjpeg": self.frame_kind == "mjpeg_raw", "raw_jpeg": self.raw_jpeg,
            "driver": info["driver"], "bus": info["bus"],
            "device_id": device_id,
        }
        log.info("USB-камера %d: %s (SN: %s) %dx%d @ %.1f fps, %s",
                 device_id, model, info["serial"], real_w, real_h, real_fps,
                 "сырой MJPG (без декодирования)" if self.raw_jpeg
                 else "декодированные кадры BGR")

    def _enable_raw_jpeg(self) -> bool:
        """Включает CONVERT_RGB=0 и проверяет, что приходят JPEG-пакеты."""
        try:
            fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
            name = "".join(chr((fourcc >> (8 * i)) & 0xFF)
                           for i in range(4))
            if name != "MJPG":
                log.info("USB %d отдаёт %r, не MJPG — сырой режим выключен",
                         self.device_id, name)
                return False
            self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
            for _ in range(5):
                ok, fr = self.cap.read()
                if (ok and fr is not None and fr.size > 2
                        and fr.reshape(-1)[:2].tolist() == [0xFF, 0xD8]):
                    return True
        except Exception:
            log.debug("raw jpeg", exc_info=True)
        log.warning("USB %d: сырые JPEG-пакеты не получены, "
                    "использую обычное декодирование", self.device_id)
        try:
            self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        except Exception:
            pass
        return False

    def _try_enable_passthrough(self):
        fcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        name = "".join(chr((fcc >> (8 * i)) & 0xFF) for i in range(4))
        if name != "MJPG":
            log.info("USB-камера %d отдаёт %r, а не MJPG — запись через "
                     "перекодирование", self.device_id, name)
            return
        self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
        ok, frame = self.cap.read()
        if (ok and frame is not None and frame.ndim == 2
                and frame.shape[0] == 1 and frame.size > 4
                and frame.reshape(-1)[0] == 0xFF
                and frame.reshape(-1)[1] == 0xD8):
            self.frame_kind = "mjpeg_raw"
            log.info("USB-камера %d: режим MJPG без перекодирования",
                     self.device_id)
        else:
            self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
            log.warning("USB-камера %d: сырые JPEG-кадры недоступны, "
                        "использую декодированные", self.device_id)

    def to_image(self, frame: np.ndarray) -> Optional[np.ndarray]:
        if self.frame_kind == "mjpeg_raw":
            return cv2.imdecode(frame.reshape(-1), cv2.IMREAD_COLOR)
        return frame

    def get_frame(self, timeout_ms: int = 200) -> Optional[np.ndarray]:
        cap = self.cap
        if cap is None or not cap.isOpened():
            time.sleep(timeout_ms / 1000.0)
            return None
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01)
            return None
        return frame

    def release(self):
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
                log.info("USB-камера %d освобождена", self.device_id)
            except Exception as e:
                log.warning("Ошибка освобождения USB-камеры: %s", e)
            gc.collect()

    def get_info(self) -> Dict:
        return self._info


# ============ GigE / U3V через Aravis ============
class ArvCameraManager(CameraInterface):
    """Камера через Aravis. Кадры — Mono8 (2D uint8), без cvtColor."""

    def __init__(self, arv_device_id: Optional[str] = None,
                 pixel_format: str = "Mono8",
                 settings: Optional[Settings] = None):
        if Aravis is None:
            raise RuntimeError(
                "Aravis недоступен. Установите gir1.2-aravis-0.8 "
                "(или соберите 0.10) и python3-gi")
        self.s = settings or Settings()
        self.camera = None
        self.stream = None
        self.pixel_format = pixel_format
        self.arv_device_id = arv_device_id
        self._warned_format = False
        self._info = {"name": "Unknown GenICam", "serial": "Unknown",
                      "ip": "Unknown", "status": "Disconnected"}
        self._connect()

    def _connect(self):
        try:
            if self.arv_device_id is None:
                Aravis.update_device_list()
                if Aravis.get_n_devices() == 0:
                    raise RuntimeError("Камеры Aravis не найдены")
                self.arv_device_id = Aravis.get_device_id(0)

            # Подключаемся по id (стабилен между сканированиями), а не по
            # индексу: индекс меняется, если порядок обнаружения сменился.
            self.camera = Aravis.Camera.new(self.arv_device_id)
            cam = self.camera
            device = cam.get_device()

            ip = "N/A"
            try:
                if cam.is_gv_device():
                    ok, ip_obj, _mask, _gw = device.get_current_ip()
                    ip = ip_obj.to_string()
            except Exception:
                pass

            self._info = {
                "name": cam.get_model_name(),
                "serial": cam.get_device_serial_number(),
                "ip": ip, "status": "Connected",
            }
            log.info("Aravis: %s (SN: %s, IP: %s)", self._info["name"],
                     self._info["serial"], ip)

            # Размер пакета под MTU сетевой карты (для GigE).
            try:
                if cam.is_gv_device():
                    size = cam.gv_auto_packet_size()
                    log.info("GigE packet size = %s", size)
            except Exception as e:
                log.warning("gv_auto_packet_size: %s", e)

            if self.s.gige_target_fps:
                try:
                    device.set_boolean_feature_value(
                        "AcquisitionFrameRateEnable", True)
                    device.set_float_feature_value(
                        "AcquisitionFrameRate",
                        float(self.s.gige_target_fps))
                    log.info("AcquisitionFrameRate = %s",
                             device.get_float_feature_value(
                                 "AcquisitionFrameRate"))
                except Exception as e:
                    log.warning("Не удалось задать частоту кадров: %s", e)

            if self.pixel_format:
                try:
                    cam.set_pixel_format_from_string(self.pixel_format)
                except Exception as e:
                    log.warning("Не удалось задать формат %s: %s",
                                self.pixel_format, e)
            log.info("Формат пикселей: %s",
                     cam.get_pixel_format_as_string())

            try:
                cam.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
            except Exception:
                pass

            self.stream = cam.create_stream(None, None)
            try:
                self.stream.set_property("packet-timeout", 50000)
                self.stream.set_property("initial-packet-timeout", 5000)
            except Exception as e:
                log.warning("Таймауты потока не заданы: %s", e)

            payload = cam.get_payload()
            for _ in range(self.s.gige_buffers):
                self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))

            cam.start_acquisition()
            self._info["fps"] = self._safe(cam.get_frame_rate)

        except Exception as e:
            log.error("Ошибка подключения к камере Aravis: %s", e)
            self._info["status"] = f"Error: {str(e)[:50]}"
            self.release()
            raise

    @staticmethod
    def _safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    def get_frame(self, timeout_ms: int = 200) -> Optional[np.ndarray]:
        stream = self.stream
        if stream is None:
            time.sleep(timeout_ms / 1000.0)
            return None

        buffer = stream.timeout_pop_buffer(int(timeout_ms) * 1000)
        if buffer is None:
            return None

        frame = None
        try:
            if buffer.get_status() == Aravis.BufferStatus.SUCCESS:
                data = buffer.get_data()
                try:
                    width = buffer.get_image_width()
                    height = buffer.get_image_height()
                except AttributeError:
                    _, _, width, height = buffer.get_image_region()

                if len(data) == width * height:
                    # get_data() отдаёт копию (bytes), так что буфер можно
                    # сразу вернуть в пул — кадр не перезапишется.
                    frame = np.frombuffer(data, dtype=np.uint8).reshape(
                        (height, width))
                elif not self._warned_format:
                    self._warned_format = True
                    log.error(
                        "Размер данных %d не соответствует Mono8 %dx%d — "
                        "проверьте PixelFormat камеры", len(data),
                        width, height)
        except Exception as e:
            log.error("Ошибка преобразования кадра: %s", e)
            frame = None
        finally:
            try:
                stream.push_buffer(buffer)
            except Exception:
                pass
        return frame

    def get_stats(self) -> Dict:
        """Счётчики потока Aravis (потери — см. README, «Диагностика»)."""
        out = {}
        if self.stream is None:
            return out
        try:
            completed, failures, underruns = self.stream.get_statistics()
            out.update(completed=completed, failures=failures,
                       underruns=underruns)
        except Exception:
            pass
        for prop in ("n-missing-packets", "n-resend-requests",
                     "n-ignored-packets"):
            try:
                out[prop.replace("-", "_")] = self.stream.get_property(prop)
            except Exception:
                pass
        return out

    def release(self):
        cam, stream = self.camera, self.stream
        self.camera = None
        self.stream = None
        if cam is None:
            return
        try:
            if stream is not None:
                try:
                    stats = {}
                    completed, failures, underruns = stream.get_statistics()
                    stats = dict(completed=completed, failures=failures,
                                 underruns=underruns)
                    log.info("Статистика потока: %s", stats)
                except Exception:
                    pass
            cam.stop_acquisition()
            if stream is not None:
                stream.set_emit_signals(False)
            log.info("Камера Aravis освобождена")
        except Exception as e:
            log.warning("Ошибка освобождения камеры Aravis: %s", e)
        gc.collect()

    def get_info(self) -> Dict:
        return self._info


# ============ СКАНЕР КАМЕР ============
@contextlib.contextmanager
def _stderr_suppressed():
    """Глушит stderr на уровне fd (OpenCV/V4L2 шумят при пробе устройств)."""
    saved = devnull = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        os.dup2(devnull, 2)
    except Exception:
        saved = None
    try:
        yield
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 2)
                os.close(saved)
            except Exception:
                pass
        if devnull is not None:
            try:
                os.close(devnull)
            except Exception:
                pass


class CameraScanner:
    @staticmethod
    def scan_aravis_cameras() -> List[Dict]:
        """
        Список камер Aravis БЕЗ их открытия: берём данные из таблицы
        обнаружения. Открытие камеры забирает у других клиентов control
        privilege и заметно замедляет сканирование.
        """
        cameras: List[Dict] = []
        if Aravis is None:
            return cameras
        try:
            Aravis.update_device_list()
            n = Aravis.get_n_devices()
            if n == 0:
                log.info("Камеры Aravis не найдены")
                return cameras

            def attr(fn_name, idx, default="N/A"):
                try:
                    v = getattr(Aravis, fn_name)(idx)
                    return v if v else default
                except Exception:
                    return default

            for idx in range(n):
                try:
                    dev_id = Aravis.get_device_id(idx)
                    model = attr("get_device_model", idx, "GenICam Camera")
                    serial = attr("get_device_serial_nbr", idx)
                    address = attr("get_device_address", idx)
                    log.info("Найдена камера Aravis: %s (SN: %s, %s)",
                             model, serial, address)
                    cameras.append({
                        "name": f"{model} [{idx}]",
                        "serial": serial,
                        "ip": address,
                        "status": "Available",
                        "type": "aravis",
                        "device_id": idx,
                        "arv_id": dev_id,
                    })
                except Exception as e:
                    log.error("Ошибка чтения камеры Aravis #%d: %s", idx, e)
        except Exception as e:
            log.error("Ошибка сканирования Aravis: %s", e)
        return cameras

    @staticmethod
    def scan_usbcams(max_devices: int = 10) -> List[Dict]:
        cameras: List[Dict] = []
        existing = []
        if os.path.isdir("/dev"):
            for name in os.listdir("/dev"):
                if name.startswith("video") and name[5:].isdigit():
                    idx = int(name[5:])
                    if idx < max_devices:
                        existing.append(idx)
        existing.sort()

        with _stderr_suppressed():
            for device_id in existing:
                cap = None
                try:
                    cap = _open_v4l2(device_id)
                    if not cap or not cap.isOpened():
                        continue
                    ret, frame = cap.read()
                    if not ret or frame is None:
                        continue
                    h, w = frame.shape[:2]
                    info = (_v4l2_info(device_id) if os.name != "nt" else
                            {"model": "N/A", "serial": "N/A"})
                    model = (info["model"] if info["model"] != "N/A"
                             else f"USB Camera {device_id}")
                    log.info("Найдена USB-камера %d: %s (SN: %s) %dx%d",
                             device_id, model, info["serial"], w, h)
                    cameras.append({
                        "name": model, "serial": info["serial"],
                        "ip": "N/A", "status": "Available",
                        "type": "usbcam", "device_id": device_id,
                        "width": w, "height": h,
                    })
                except Exception:
                    log.debug("probe video%d", device_id, exc_info=True)
                finally:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                    time.sleep(0.05)
        gc.collect()
        return cameras

    @staticmethod
    def scan_all() -> List[Dict]:
        arv = CameraScanner.scan_aravis_cameras()
        usb = CameraScanner.scan_usbcams()
        log.info("Найдено: GigE/Aravis %d, USB %d", len(arv), len(usb))
        return arv + usb


# ============ ФАБРИКА ============
def create_camera(camera_type: str = "aravis",
                  settings: Optional[Settings] = None,
                  **kwargs) -> CameraInterface:
    if camera_type in ("gige", "aravis"):
        return ArvCameraManager(
            arv_device_id=kwargs.get("arv_id"),
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            settings=settings)
    if camera_type == "usbcam":
        return UsbCameraManager(
            device_id=kwargs.get("device_id", 0), settings=settings)
    raise ValueError(f"Неизвестный тип камеры: {camera_type}")
