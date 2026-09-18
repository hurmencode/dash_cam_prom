import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import re
import time
from datetime import datetime


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


# ============ ВИДЕОРЕКОРДЕР (OpenCV MJPG, Mono8, защита от заполнения диска) ============
class VideoRecorder:
    """
    Простая и стабильная запись через OpenCV MJPG (isColor=False для Mono8).
    Каждый кадр независим — нет PTS-конфликтов, нет segfault'ов.
    Есть защита от заполнения диска: при свободном месте ниже min_free_mb
    запись останавливается, вызывается on_disk_full (в фоновом потоке —
    UI должен сам перекинуть обработку в main-поток).
    """

    def __init__(self, output_dir: str = "recordings", fps: int = 30,
                 is_color: bool = False,
                 min_free_mb: int = 500,
                 on_disk_full=None):
        self.output_dir = output_dir
        self.fps = max(1, int(fps))
        self.is_color = is_color
        self.min_free_mb = int(min_free_mb)
        self.on_disk_full = on_disk_full

        self.writer = None
        self.is_recording = False
        self.record_path = None
        self.recording_start_time = None
        self.frame_count = 0

        # Служебное для проверки диска
        self._last_disk_check = 0.0
        self._disk_check_interval = 1.0
        self._disk_full_triggered = False

        os.makedirs(output_dir, exist_ok=True)

    # ---------- Публичные методы ----------
    def start_recording(self, width: int, height: int,
                        camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path

        # Проверка места ДО старта
        free_mb = self._free_mb()
        if free_mb < self.min_free_mb:
            raise RuntimeError(
                f"Мало места на диске: {free_mb:.0f} MB "
                f"(нужно минимум {self.min_free_mb} MB)\n"
            )

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
        filename = f"{safe_name}_{timestamp}_mjpg.avi"
        self.record_path = os.path.join(self.output_dir, filename)

        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        self.writer = cv2.VideoWriter(
            self.record_path, fourcc, self.fps,
            (width, height), isColor=self.is_color
        )

        if not self.writer.isOpened():
            raise RuntimeError(
                f"Failed to create MJPG writer: {self.record_path}\n"
            )

        self.is_recording = True
        self.recording_start_time = time.time()
        self.frame_count = 0
        self._last_disk_check = 0.0
        self._disk_full_triggered = False

        print(f"Запись начата (MJPG): {self.record_path}\n")
        print(f"   {width}x{height}, isColor={self.is_color}, "
              f"fps={self.fps}, min_free={self.min_free_mb} MB\n")
        return self.record_path

    def write_frame(self, frame: np.ndarray) -> bool:
        if self.writer is None or not self.is_recording:
            return False
        if frame is None:
            return False

        # --- Проверка свободного места не чаще раза в секунду ---
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
                    # Меняем флаг ДО callback'а, чтобы избежать гонок
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
            print(f"   Кадров: {self.frame_count}\n")
            print(f"   Длительность: {duration:.1f} сек\n")
            print(f"   Размер видео: {file_size:.2f} MB\n")
            print(f"   Остановлено по диску: {self._disk_full_triggered}\n")
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
            'codec': 'MJPG',
            'is_color': self.is_color,
            'min_free_mb': self.min_free_mb,
            'disk_full_triggered': self._disk_full_triggered,
            'free_mb': self._free_mb()
        }

    # ---------- Служебные ----------
    def _free_mb(self) -> float:
        """Свободное место в директории записи, МБ."""
        try:
            st = os.statvfs(self.output_dir)
            return (st.f_bavail * st.f_frsize) / (1024 * 1024)
        except Exception as e:
            print(f"statvfs error: {e}\n")
            return float('inf')


# ============ LUCID CAMERA MANAGER (ARENA API) ============
class LucidCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8',
                 device_index: int = 0, saved_ip: str = None):
        try:
            from arena_api.system import system
            self.system = system
        except ImportError:
            raise RuntimeError(
                "Arena SDK не установлен. Проверь arena_api.\n"
            )

        self.camera = None
        self.pixel_format = pixel_format
        self.device_index = device_index
        self.saved_ip = saved_ip

        self._info = {
            'name': 'Unknown Lucid',
            'serial': 'Unknown',
            'ip': saved_ip if saved_ip else 'Unknown',
            'status': 'Disconnected'
        }
        self._connect_camera()

    def _get_camera_ip(self) -> str:
        try:
            if self.saved_ip and self.saved_ip != 'Unknown':
                return self.saved_ip
            for node_name in ('DeviceIPAddress', 'GevCurrentIPAddress',
                              'GevPersistentIPAddress'):
                try:
                    node = self.camera.nodemap.get_node(node_name)
                    if node:
                        value = node.value
                        if isinstance(value, int):
                            ip_bytes = value.to_bytes(4, byteorder='big')
                            return '.'.join(str(b) for b in ip_bytes)
                        elif isinstance(value, str):
                            return value
                except Exception:
                    continue
            return 'Unknown'
        except Exception as e:
            print(f"Error getting camera IP: {e}\n")
            return 'Unknown'

    def _connect_camera(self):
        try:
            devices = self.system.create_device()
            if not devices:
                raise RuntimeError("No Lucid Triton cameras found.\n")

            if self.device_index < len(devices):
                self.camera = devices[self.device_index]
            else:
                self.camera = devices[0]

            try:
                model_name = self.camera.nodemap.get_node(
                    'DeviceModelName').value
                serial = self.camera.nodemap.get_node(
                    'DeviceSerialNumber').value
                ip_address = self._get_camera_ip()

                self._info = {
                    'name': model_name,
                    'serial': serial,
                    'ip': ip_address,
                    'status': 'Connected'
                }
                print(f"[SUCCESS] Connected: {model_name} "
                      f"(SN: {serial}, IP: {ip_address})\n")
            except Exception as e:
                print(f"[WARN] Error reading camera info: {e}\n")
                self._info['status'] = 'Connected (Info Error)'

            if self.pixel_format:
                try:
                    self.camera.nodemap.get_node('PixelFormat').value = \
                        self.pixel_format
                except Exception as e:
                    print(f"[WARN] Failed to set pixel format "
                          f"{self.pixel_format}: {e}\n")

            self.camera.start_stream()

        except Exception as e:
            print(f"[ERROR] Error connecting to Lucid camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise

    def get_frame(self) -> Optional[np.ndarray]:
        """Mono8 (2D uint8). Без cvtColor — быстро."""
        if not self.camera:
            return None

        buffer = self.camera.get_buffer()
        if buffer is None:
            return None

        try:
            height, width = buffer.height, buffer.width
            img_array = np.ctypeslib.as_array(
                buffer.pdata, shape=(height, width)
            )
            frame = np.array(img_array)
        except Exception as e:
            print(f"[ERROR] Frame conversion error: {e}\n")
            frame = None
        finally:
            self.camera.requeue_buffer(buffer)

        return frame

    def release(self):
        if self.camera:
            try:
                self.camera.stop_stream()
                self.system.destroy_device()
                print("[INFO] Lucid camera stream stopped, device released.")
            except Exception as e:
                print(f"[WARN] Error closing camera: {e}")

    def get_info(self) -> Dict:
        return self._info


# ============ СКАНЕР КАМЕР ============
class CameraScanner:
    @staticmethod
    def _extract_ip_from_device(device) -> str:
        try:
            for node_name in ('DeviceIPAddress', 'GevCurrentIPAddress'):
                try:
                    node = device.nodemap.get_node(node_name)
                    if node:
                        value = node.value
                        if isinstance(value, int):
                            ip_bytes = value.to_bytes(4, byteorder='big')
                            return '.'.join(str(b) for b in ip_bytes)
                        elif isinstance(value, str):
                            return value
                except Exception:
                    pass
            return 'Unknown'
        except Exception:
            return 'Unknown'

    @staticmethod
    def scan_lucid_cameras() -> list:
        cameras = []
        seen_serials = set()

        try:
            from arena_api.system import system
            devices = system.create_device()
            if not devices:
                print("No Lucid devices found\n")
                return cameras

            for idx, device in enumerate(devices):
                try:
                    model_name = device.nodemap.get_node(
                        'DeviceModelName').value
                    serial = device.nodemap.get_node(
                        'DeviceSerialNumber').value
                    ip_address = CameraScanner._extract_ip_from_device(device)

                    if serial in seen_serials:
                        print(f"[INFO] Skipping duplicate: {serial}\n")
                        continue
                    seen_serials.add(serial)

                    print(f"Found Lucid camera: {model_name} "
                          f"(SN: {serial}, IP: {ip_address})\n")

                    cameras.append({
                        'name': model_name,
                        'serial': serial,
                        'ip': ip_address,
                        'status': 'Available',
                        'type': 'lucid',
                        'device_id': idx,
                        'device': device,
                        '_saved_ip': ip_address
                    })
                except Exception as e:
                    print(f"Error reading Lucid camera info: {e}\n")

            system.destroy_device()
        except ImportError:
            print("Arena SDK not installed\n")
        except Exception as e:
            print(f"Error scanning Lucid cameras: {e}\n")
        return cameras

    @staticmethod
    def scan_all() -> list:
        print("Scanning Lucid cameras...\n")
        lucid_cams = CameraScanner.scan_lucid_cameras()
        print(f"Found {len(lucid_cams)} Lucid cameras\n")
        return lucid_cams


# ============ ФАБРИКА ============
def create_camera(camera_type: str = "lucid", **kwargs):
    if camera_type == "lucid":
        return LucidCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=kwargs.get("saved_ip", None)
        )
    else:
        raise ValueError(f"Unknown camera type: {camera_type}. "
                         f"Only 'lucid' is supported.")