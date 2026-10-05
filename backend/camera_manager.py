import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import re
import time
import gc
import threading
import subprocess
from datetime import datetime

# Импортируем Aravis
import gi
gi.require_version('Aravis', '0.10')
from gi.repository import Aravis


# ============ БАЗОВЫЙ ИНТЕРФЕЙС ============
class CameraInterface(ABC):
    @abstractmethod
    def get_frame(self) -> Optional[np.ndarray]:
        pass

    @abstractmethod
    def release(self):
        pass

    @abstractmethod
    def get_info(self) -> Dict:
        pass


# ============ ВИДЕОРЕКОРДЕР (FFV1 lossless, fallback MJPG) ============
class VideoRecorder:
    """
    Запись одного сегмента видео.
    Основной кодек — FFV1 (lossless), fallback — MJPG.

    Для Mono8-камер (GigE) isColor=False, для USB-камер (BGR) isColor=True.
    Есть защита от заполнения диска: при свободном месте ниже
    min_free_mb запись останавливается, вызывается on_disk_full.
    """

    CODEC_PRIORITY = ['FFV1', 'MJPG']

    def __init__(self, output_dir: str = "recordings", fps: int = 30,
                 is_color: bool = False,
                 min_free_mb: int = 1000,
                 on_disk_full=None):
        self.output_dir = output_dir
        self.fps = max(1, int(fps))
        self.is_color = is_color
        self.min_free_mb = int(min_free_mb)
        self.on_disk_full = on_disk_full

        self.codec = None
        self.writer = None
        self.is_recording = False
        self.record_path = None
        self.recording_start_time = None
        self.frame_count = 0

        self._last_disk_check = 0.0
        self._disk_check_interval = 1.0
        self._disk_full_triggered = False

        os.makedirs(output_dir, exist_ok=True)

    def start_recording(self, width: int, height: int,
                        camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path

        free_mb = self._free_mb()
        if free_mb < self.min_free_mb:
            raise RuntimeError(
                f"Мало места на диске: {free_mb:.0f} MB "
                f"(нужно минимум {self.min_free_mb} MB)\n"
            )

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)

        writer = None
        chosen_codec = None
        chosen_path = None

        for codec in self.CODEC_PRIORITY:
            filename = f"{safe_name}_{timestamp}_{codec}.avi"
            path = os.path.join(self.output_dir, filename)
            fourcc = cv2.VideoWriter_fourcc(*codec)
            try:
                w = cv2.VideoWriter(
                    path, fourcc, self.fps,
                    (width, height), isColor=self.is_color
                )
                if w.isOpened():
                    writer = w
                    chosen_codec = codec
                    chosen_path = path
                    break
                else:
                    w.release()
                    print(f"[WARN] Кодек {codec} не открылся, "
                          f"пробую следующий\n")
            except Exception as e:
                print(f"[WARN] Ошибка с кодеком {codec}: {e}\n")

        if writer is None:
            raise RuntimeError(
                f"Не удалось создать VideoWriter ни с одним из "
                f"кодеков: {self.CODEC_PRIORITY}\n"
            )

        self.writer = writer
        self.codec = chosen_codec
        self.record_path = chosen_path
        self.is_recording = True
        self.recording_start_time = time.time()
        self.frame_count = 0
        self._last_disk_check = 0.0
        self._disk_full_triggered = False

        print(f"Запись начата ({self.codec}): {self.record_path}\n")
        print(f"   {width}x{height}, isColor={self.is_color}, "
              f"fps={self.fps}, min_free={self.min_free_mb} MB\n")
        return self.record_path

    def write_frame(self, frame: np.ndarray) -> bool:
        if self.writer is None or not self.is_recording:
            return False
        if frame is None:
            return False

        now = time.time()
        if now - self._last_disk_check >= self._disk_check_interval:
            self._last_disk_check = now
            free_mb = self._free_mb()
            if free_mb < self.min_free_mb:
                if not self._disk_full_triggered:
                    self._disk_full_triggered = True
                    print(f"!!! Мало места на диске "
                          f"({free_mb:.0f} MB < {self.min_free_mb} MB). "
                          f"Останавливаю запись.\n")
                    self.is_recording = False
                    if self.on_disk_full is not None:
                        try:
                            self.on_disk_full(free_mb)
                        except Exception as e:
                            print(f"on_disk_full callback error: {e}\n")
                return False

        try:
            self.writer.write(frame)
            self.frame_count += 1
            return True
        except Exception as e:
            print(f"Error writing frame: {e}\n")
            return False

    def stop_recording(self) -> Optional[str]:
        if self.writer is None:
            return None

        self.is_recording = False
        time.sleep(0.2)

        if self.writer:
            self.writer.release()
            self.writer = None

            duration = (time.time() - self.recording_start_time
                        if self.recording_start_time else 0)
            try:
                file_size = os.path.getsize(self.record_path) / (1024 * 1024)
            except OSError:
                file_size = 0.0

            print(f"Запись остановлена: {self.record_path}\n")
            print(f"   Кодек: {self.codec}\n")
            print(f"   Кадров: {self.frame_count}\n")
            print(f"   Длительность: {duration:.1f} сек\n")
            print(f"   Размер видео: {file_size:.2f} MB\n")
            return self.record_path

        return None

    def get_recording_status(self) -> Dict:
        return {
            'is_recording': self.is_recording,
            'file_path': self.record_path,
            'duration': (time.time() - self.recording_start_time
                         if self.recording_start_time else 0),
            'fps': self.fps,
            'frame_count': self.frame_count,
            'codec': self.codec,
            'is_color': self.is_color,
        }

    def _free_mb(self) -> float:
        try:
            st = os.statvfs(self.output_dir)
            return (st.f_bavail * st.f_frsize) / (1024 * 1024)
        except Exception as e:
            print(f"statvfs error: {e}\n")
            return float('inf')


# ============ СЕГМЕНТИРОВАННЫЙ РЕКОРДЕР ============
class SegmentingRecorder:
    """
    Обёртка над VideoRecorder, которая каждые segment_seconds
    закрывает текущий файл и открывает новый.

    Для 12-часовой записи с segment_seconds=3600 получится
    12 файлов примерно одинакового размера.

    При выключении питания теряется максимум один сегмент
    (текущий), все закрытые остаются целыми.
    """

    def __init__(self, output_dir: str, fps: int, is_color: bool,
                 min_free_mb: int = 1000,
                 on_disk_full=None,
                 segment_seconds: int = 3600,
                 camera_name: str = "camera",
                 on_segment_change=None):
        self.output_dir = output_dir
        self.fps = fps
        self.is_color = is_color
        self.min_free_mb = min_free_mb
        self.on_disk_full = on_disk_full
        self.segment_seconds = max(60, int(segment_seconds))
        self.camera_name = camera_name
        self.on_segment_change = on_segment_change

        self._recorder = None
        self._width = None
        self._height = None
        self._segment_start_time = None
        self._segment_index = 0
        self._segment_paths = []
        self._is_recording = False
        self._stop_requested = False

        self._thread = None
        self._thread_lock = threading.Lock()

    # ---------- Публичные ----------
    def start(self, width: int, height: int) -> bool:
        if self._is_recording:
            return False

        self._width = width
        self._height = height
        self._segment_index = 0
        self._segment_paths = []
        self._stop_requested = False
        self._is_recording = True

        if not self._start_new_segment():
            self._is_recording = False
            return False

        self._thread = threading.Thread(
            target=self._segment_loop, daemon=True
        )
        self._thread.start()
        return True

    def write_frame(self, frame: np.ndarray) -> bool:
        if not self._is_recording or self._recorder is None:
            return False
        return self._recorder.write_frame(frame)

    def stop(self) -> List[str]:
        """Останавливает запись, возвращает список путей всех сегментов."""
        if not self._is_recording and self._recorder is None:
            return list(self._segment_paths)

        self._is_recording = False
        self._stop_requested = True

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        if self._recorder is not None:
            try:
                self._recorder.stop_recording()
            except Exception as e:
                print(f"[WARN] Error stopping segment: {e}\n")
            self._recorder = None

        return list(self._segment_paths)

    def get_segment_count(self) -> int:
        return self._segment_index

    def get_segment_paths(self) -> List[str]:
        return list(self._segment_paths)

    def get_elapsed_in_segment(self) -> float:
        if self._segment_start_time is None:
            return 0.0
        return time.time() - self._segment_start_time

    def get_segment_remaining(self) -> float:
        if self._segment_start_time is None:
            return 0.0
        return max(0.0, self.segment_seconds -
                   (time.time() - self._segment_start_time))

    # ---------- Внутренние ----------
    def _start_new_segment(self) -> bool:
        self._segment_index += 1
        segment_name = f"{self.camera_name}_part{self._segment_index:03d}"
        try:
            self._recorder = VideoRecorder(
                output_dir=self.output_dir,
                fps=self.fps,
                is_color=self.is_color,
                min_free_mb=self.min_free_mb,
                on_disk_full=self.on_disk_full
            )
            path = self._recorder.start_recording(
                self._width, self._height, segment_name
            )
            self._segment_paths.append(path)
            self._segment_start_time = time.time()

            if self.on_segment_change is not None:
                try:
                    self.on_segment_change(self._segment_index, path)
                except Exception as e:
                    print(f"[WARN] on_segment_change error: {e}\n")

            print(f"[INFO] Сегмент {self._segment_index} открыт: {path}\n")
            return True
        except Exception as e:
            print(f"[ERROR] Could not start segment "
                  f"{self._segment_index}: {e}\n")
            self._recorder = None
            return False

    def _segment_loop(self):
        """Раз в секунду проверяем, не пора ли закрыть сегмент."""
        while self._is_recording and not self._stop_requested:
            time.sleep(1.0)

            if not self._is_recording or self._stop_requested:
                break

            elapsed = time.time() - self._segment_start_time
            if elapsed >= self.segment_seconds:
                with self._thread_lock:
                    if not self._is_recording:
                        break
                    print(f"[INFO] Сегмент {self._segment_index} "
                          f"закрыт ({elapsed:.0f} сек), открываю новый\n")
                    try:
                        self._recorder.stop_recording()
                    except Exception as e:
                        print(f"[WARN] Error closing segment: {e}\n")
                    self._recorder = None

                    if not self._start_new_segment():
                        self._is_recording = False
                        break

# ============ V4L2 INFO HELPERS ============

def _v4l2_info(device_id: int) -> dict:
    """
    Возвращает dict с model / serial / driver / bus для /dev/videoN
    через v4l2-ctl --info. Если поле не найдено — значение 'N/A'.
    """
    result = {
        'model': 'N/A',
        'serial': 'N/A',
        'driver': 'N/A',
        'bus': 'N/A',
    }
    try:
        out = subprocess.run(
            ['v4l2-ctl', '-d', f'/dev/video{device_id}', '--info'],
            capture_output=True, text=True, timeout=2.0
        )
        if out.returncode != 0:
            return result

        for line in out.stdout.splitlines():
            line = line.strip()
            if ':' not in line:
                continue
            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip()

            if key == 'card type':
                result['model'] = value
            elif key == 'bus info':
                # Пример: "usb-0000:00:14.0-1" — вытащим серийник
                # из /sys, потому что v4l2-ctl напрямую серийник
                # USB-камеры не показывает
                result['bus'] = value
            elif key == 'driver name':
                result['driver'] = value
    except FileNotFoundError:
        # v4l2-ctl не установлен
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    # Дополнительно вытащим серийник USB-устройства через /sys
    result['serial'] = _usb_serial_from_sys(device_id)
    return result


def _usb_serial_from_sys(device_id: int) -> str:
    """
    Пытается найти серийный номер USB-камеры через /sys/class/video4linux.
    Возвращает 'N/A', если не найдено.
    """
    try:
        # /sys/class/video4linux/video0/device → симлинк в USB-устройство
        base = f'/sys/class/video4linux/video{device_id}/device'
        if not os.path.exists(base):
            return 'N/A'

        # Идём вверх по дереву, ищем папку с файлом "serial"
        real = os.path.realpath(base)
        for _ in range(6):
            serial_path = os.path.join(real, 'serial')
            if os.path.isfile(serial_path):
                with open(serial_path, 'r') as f:
                    val = f.read().strip()
                    if val:
                        return val
            parent = os.path.dirname(real)
            if parent == real:
                break
            real = parent
    except Exception:
        pass
    return 'N/A'

# ============ USB CAMERA MANAGER (cv2.VideoCapture) ============
class UsbCameraManager(CameraInterface):
    """USB-камера через OpenCV VideoCapture. Возвращает BGR (3 канала)."""

    def __init__(self, device_id: int = 0, width: int = None,
                 height: int = None):
        self.device_id = device_id
        self.cap = None

        if os.name == 'nt':
            self.cap = cv2.VideoCapture(device_id, cv2.CAP_DSHOW)
        else:
            self.cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)

        if width is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height is not None:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Cannot open usbcam {device_id}")

        real_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        real_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        real_fps = self.cap.get(cv2.CAP_PROP_FPS)

        # ---- Достаём имя модели и серийник через v4l2-ctl ----
        info = _v4l2_info(device_id) if os.name != 'nt' else {
            'model': 'N/A', 'serial': 'N/A',
            'driver': 'N/A', 'bus': 'N/A'
        }

        model_name = info['model'] if info['model'] != 'N/A' \
            else f'USB Camera {device_id}'

        self._info = {
            'name': model_name,
            'serial': info['serial'],
            'ip': 'N/A',
            'status': 'Connected',
            'width': real_w,
            'height': real_h,
            'fps': real_fps,
            'is_color': True,
            'driver': info['driver'],
            'bus': info['bus'],
            'device_id': device_id,
        }
        print(f"[INFO] USB camera {device_id}: "
              f"{model_name} "
              f"(SN: {info['serial']}) "
              f"{real_w}x{real_h} @ {real_fps:.1f} fps\n")

    def get_frame(self) -> Optional[np.ndarray]:
        if not self.cap or not self.cap.isOpened():
            return None
        ret, frame = self.cap.read()
        return frame if ret else None

    def release(self):
        if self.cap:
            try:
                self.cap.release()
                print(f"[INFO] USB camera {self.device_id} released.")
            except Exception as e:
                print(f"[WARN] USB release error: {e}")
            self.cap = None
            gc.collect()

    def get_info(self) -> Dict:
        return self._info


# ============ ARV CAMERA MANAGER (ARAVIS API) ============
class ArvCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8',
                 device_index: int = 0, saved_ip: str = None,
                 target_frame_rate: float = None):
        self.camera = None
        self.stream = None
        self.pixel_format = pixel_format
        self.device_index = device_index
        self.saved_ip = saved_ip
        self.target_frame_rate = target_frame_rate

        self._info = {
            'name': 'Unknown GenICam',
            'serial': 'Unknown',
            'ip': saved_ip if saved_ip else 'Unknown',
            'status': 'Disconnected'
        }
        self._connect_camera()

    def _connect_camera(self):
        try:
            Aravis.update_device_list()
            n_devices = Aravis.get_n_devices()

            if n_devices == 0:
                raise RuntimeError("No GenICam devices found via Aravis\n")

            idx = self.device_index if self.device_index < n_devices else 0
            device_id = Aravis.get_device_id(idx)

            self.camera = Aravis.Camera.new(device_id)

            model_name = self.camera.get_model_name()
            serial = self.camera.get_device_serial_number()

            device = self.camera.get_device()
            [_, ip, mask, gateway] = device.get_current_ip()

            self._info = {
                'name': model_name,
                'serial': serial,
                'ip': ip.to_string(),
                'status': 'Connected'
            }
            print(f"Connected Aravis: {model_name} "
                  f"(SN: {serial}, IP: {ip.to_string()})\n")

            if self.target_frame_rate is not None:
                try:
                    device.set_boolean_feature_value(
                        "AcquisitionFrameRateEnable", True
                    )
                    device.set_float_feature_value(
                        "AcquisitionFrameRate",
                        float(self.target_frame_rate)
                    )
                    actual_rate = device.get_float_feature_value(
                        "AcquisitionFrameRate"
                    )
                    print(f"AcquisitionFrameRate = {actual_rate} Hz "
                          f"(запрошено {self.target_frame_rate})\n")
                except Exception as e:
                    print(f"[WARN] Could not set frame rate "
                          f"{self.target_frame_rate}: {e}\n")

            if self.pixel_format:
                try:
                    self.camera.set_pixel_format_from_string(
                        self.pixel_format
                    )
                except Exception as e:
                    print(f"Could not set pixel format "
                          f"{self.pixel_format}: {e}\n")

            self.stream = self.camera.create_stream(None, None)

            try:
                self.stream.set_property("packet-timeout", 50000)
                self.stream.set_property("initial-packet-timeout", 5000)
            except Exception as e:
                print(f"[WARN] Could not set stream timeouts: {e}\n")

            payload = self.camera.get_payload()
            for _ in range(30):
                self.stream.push_buffer(
                    Aravis.Buffer.new_allocate(payload)
                )

            self.camera.start_acquisition()

        except Exception as e:
            print(f"Error connecting to Aravis camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise

    def get_frame(self) -> Optional[np.ndarray]:
        """Mono8 (2D uint8). Без cvtColor — быстро."""
        if not self.stream:
            return None

        buffer = self.stream.timeout_pop_buffer(1000000)
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

                img_array = np.frombuffer(data, dtype=np.uint8).reshape(
                    (height, width)
                )
                frame = img_array
        except Exception as e:
            print(f"Frame conversion error: {e}\n")
            frame = None
        finally:
            try:
                self.stream.push_buffer(buffer)
            except Exception:
                pass

        return frame

    def release(self):
        if self.camera:
            try:
                self.camera.stop_acquisition()
                self.stream.set_emit_signals(False)
                self.stream = None
                self.camera = None
                print("[INFO] Aravis camera released.")
            except Exception as e:
                print(f"Error releasing Aravis camera: {e}\n")

    def get_info(self) -> Dict:
        return self._info


# ============ СКАНЕР КАМЕР ============
class CameraScanner:
    @staticmethod
    def scan_aravis_cameras() -> List[Dict]:
        cameras = []
        try:
            Aravis.update_device_list()
            n_devices = Aravis.get_n_devices()

            if n_devices == 0:
                print("No Aravis devices found\n")
                return cameras

            for idx in range(n_devices):
                try:
                    device_id = Aravis.get_device_id(idx)
                    camera = Aravis.Camera.new(device_id)

                    model_name = (
                        Aravis.get_device_model(idx)
                        if hasattr(Aravis, 'get_device_model')
                        else "GenICam Camera"
                    )
                    serial = camera.get_device_serial_number()

                    device = camera.get_device()
                    [_, ip, mask, gateway] = device.get_current_ip()

                    print(f"Found Aravis camera: {model_name} "
                          f"(SN: {serial}, IP: {ip.to_string()})\n")

                    cameras.append({
                        'name': f"{model_name} [{idx}]",
                        'serial': serial,
                        'ip': ip.to_string(),
                        'status': 'Available',
                        'type': 'aravis',
                        'device_id': idx,
                        '_saved_ip': ip.to_string()
                    })
                except Exception as e:
                    print(f"Error reading Aravis camera info "
                          f"at index {idx}: {e}\n")

        except Exception as e:
            print(f"Error scanning Aravis cameras: {e}\n")

        return cameras

    @staticmethod
    def scan_usbcams(max_devices: int = 10) -> List[Dict]:
        cameras = []

        existing_devices = []
        if os.path.isdir('/dev'):
            for name in os.listdir('/dev'):
                if name.startswith('video'):
                    suffix = name[len('video'):]
                    if suffix.isdigit():
                        idx = int(suffix)
                        if idx < max_devices:
                            existing_devices.append(idx)
        existing_devices.sort()

        saved_stderr_fd = None
        devnull_fd = None
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            saved_stderr_fd = os.dup(2)
            os.dup2(devnull_fd, 2)
        except Exception as e:
            print(f"[WARN] Could not suppress stderr: {e}")
            saved_stderr_fd = None

        try:
            for device_id in existing_devices:
                cap = None
                try:
                    if os.name == 'nt':
                        cap = cv2.VideoCapture(device_id, cv2.CAP_DSHOW)
                    else:
                        cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)

                    if not cap or not cap.isOpened():
                        continue

                    ret, frame = cap.read()
                    if not ret or frame is None:
                        continue

                    h, w = frame.shape[:2]

                    # Достаём модель и серийник
                    info = _v4l2_info(device_id) if os.name != 'nt' else {
                        'model': 'N/A', 'serial': 'N/A'
                    }
                    model_name = info['model'] if info['model'] != 'N/A' \
                        else f'USB Camera {device_id}'

                    print(f"Found USB camera {device_id}: "
                        f"{model_name} "
                        f"(SN: {info['serial']}) "
                        f"{w}x{h}\n")

                    cameras.append({
                        'name': model_name,
                        'serial': info['serial'],
                        'ip': 'N/A',
                        'status': 'Available',
                        'type': 'usbcam',
                        'device_id': device_id,
                        '_saved_ip': None,
                        'width': w,
                        'height': h
                    })
                except Exception:
                    pass
                finally:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
                        del cap
                        gc.collect()
                    time.sleep(0.05)
        finally:
            if saved_stderr_fd is not None:
                try:
                    os.dup2(saved_stderr_fd, 2)
                    os.close(saved_stderr_fd)
                except Exception:
                    pass
            if devnull_fd is not None:
                try:
                    os.close(devnull_fd)
                except Exception:
                    pass

        return cameras

    @staticmethod
    def scan_all() -> List[Dict]:
        all_cameras = []

        print("Scanning Aravis cameras...\n")
        arv_cams = CameraScanner.scan_aravis_cameras()
        all_cameras.extend(arv_cams)
        print(f"Found {len(arv_cams)} Aravis cameras\n")

        print("Scanning USB cameras...\n")
        usb_cams = CameraScanner.scan_usbcams()
        all_cameras.extend(usb_cams)
        print(f"Found {len(usb_cams)} USB cameras\n")

        return all_cameras


# ============ ФАБРИКА ============
def create_camera(camera_type: str = "aravis", **kwargs) -> CameraInterface:
    if camera_type in ("gige", "aravis"):
        saved_ip = kwargs.get("saved_ip", None)
        return ArvCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=saved_ip,
            target_frame_rate=kwargs.get("target_frame_rate", None)
        )
    elif camera_type == "usbcam":
        return UsbCameraManager(device_id=kwargs.get("device_id", 0))
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")