import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import platform
import re
import time
from datetime import datetime
import threading
import queue

# Импортируем Aravis
import gi
gi.require_version('Aravis', '0.10')
from gi.repository import Aravis

# Оптимизация OpenCV под многоядерный CPU Jetson
cv2.setNumThreads(4)


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


# ============ ВИДЕОРЕКОРДЕР (только XVID, асинхронная запись) ============
class VideoRecorder:
    """
    Рекордер только в XVID (lossy).
    Запись вынесена в отдельный поток с очередью — не блокирует захват.
    """

    def __init__(self, output_dir: str = "recordings", fps: int = 30):
        self.output_dir = output_dir
        self.fps = max(1, int(fps))
        self.writer = None
        self.is_recording = False
        self.record_path = None
        self.frame_width = None
        self.frame_height = None
        self.recording_start_time = None
        self.frame_count = 0

        # Асинхронная очередь записи
        self._write_queue = queue.Queue(maxsize=120)
        self._writer_thread = None
        self._writer_running = False
        self._dropped_frames = 0

        os.makedirs(output_dir, exist_ok=True)

    # ---------- публичные методы ----------
    def start_recording(self, width: int, height: int, camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path

        self.frame_width = width
        self.frame_height = height
        self.frame_count = 0
        self._dropped_frames = 0

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
        filename = f"{safe_name}_{timestamp}_xvid.avi"
        self.record_path = os.path.join(self.output_dir, filename)

        # XVID
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        self.writer = cv2.VideoWriter(
            self.record_path,
            fourcc,
            self.fps,
            (width, height)
        )

        # Fallback на MJPG если XVID недоступен
        if not self.writer.isOpened():
            print("XVID недоступен, используем MJPG\n")
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = cv2.VideoWriter(
                self.record_path, fourcc, self.fps, (width, height)
            )

        if not self.writer.isOpened():
            raise RuntimeError(f"Failed to create video writer: {self.record_path}\n")

        self.is_recording = True
        self.recording_start_time = time.time()

        # Запускаем поток-писатель
        self._writer_running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True
        )
        self._writer_thread.start()

        print(f"Запись начата: {self.record_path}\n")
        print(f"   Кодек: XVID, FPS: {self.fps}\n")
        return self.record_path

    def write_frame(self, frame):
        if not self.is_recording or self.writer is None:
            return False
        # Пишем прямо в текущем потоке, без отдельного writer-thread
        self.writer.write(frame)
        self.frame_count += 1
        return True

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None

        self.is_recording = False

        # Ждём, пока очередь опустеет (макс 5 сек)
        deadline = time.time() + 5.0
        while not self._write_queue.empty() and time.time() < deadline:
            time.sleep(0.05)

        self._writer_running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=3)
            self._writer_thread = None

        if self.writer:
            self.writer.release()
            self.writer = None

            duration = time.time() - self.recording_start_time
            file_size = os.path.getsize(self.record_path) / (1024 * 1024)

            print(f"Запись остановлена: {self.record_path}\n")
            print(f"   Кадров: {self.frame_count}\n")
            print(f"   Дропнуто: {self._dropped_frames}\n")
            print(f"   Длительность: {duration:.1f} сек\n")
            print(f"   Размер видео: {file_size:.1f} MB\n")
            return self.record_path

        return None

    def get_recording_status(self) -> Dict:
        return {
            'is_recording': self.is_recording,
            'file_path': self.record_path,
            'duration': time.time() - self.recording_start_time if self.recording_start_time else 0,
            'fps': self.fps,
            'frame_count': self.frame_count,
            'dropped_frames': self._dropped_frames,
            'fourcc': 'XVID'
        }

    # ---------- внутренний поток ----------
    def _writer_loop(self):
        """Пишет кадры в файл в отдельном потоке."""
        while self._writer_running or not self._write_queue.empty():
            try:
                frame = self._write_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if self.writer is not None:
                    self.writer.write(frame)
                    self.frame_count += 1
            except Exception as e:
                print(f"Error writing frame: {e}\n")
            finally:
                self._write_queue.task_done()


# ============ ARV CAMERA MANAGER (ARAVIS API) ============
class ArvCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8', device_index: int = 0, saved_ip: str = None):
        self.camera = None
        self.stream = None
        self.pixel_format = pixel_format
        self.device_index = device_index
        self.saved_ip = saved_ip

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
            print(f"Connected Aravis: {model_name} (SN: {serial}, IP: {ip.to_string()})\n")

            if self.pixel_format:
                try:
                    self.camera.set_pixel_format_from_string(self.pixel_format)
                except Exception as e:
                    print(f"Could not set pixel format {self.pixel_format}: {e}\n")

            self.stream = self.camera.create_stream(None, None)
            payload = self.camera.get_payload()
            for _ in range(10):  # больше буферов — меньше пропусков
                self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))

            self.camera.start_acquisition()

        except Exception as e:
            print(f"Error connecting to Aravis camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise

    def get_frame(self) -> Optional[np.ndarray]:
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

                img_array = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
                frame = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)
        except Exception as e:
            print(f"Frame conversion error: {e}\n")
            frame = None
        finally:
            self.stream.push_buffer(buffer)

        return frame

    def release(self):
        if self.camera:
            try:
                self.camera.stop_acquisition()
                self.stream.set_emit_signals(False)
                self.stream = None
                self.camera = None
            except Exception as e:
                print(f"Error releasing Aravis camera: {e}\n")

    def get_info(self) -> Dict:
        return self._info


# ============ СКАНЕР КАМЕР (только Aravis) ============
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

                    model_name = Aravis.get_device_model(idx) if hasattr(Aravis, 'get_device_model') else "GenICam Camera"
                    serial = camera.get_device_serial_number()

                    device = camera.get_device()
                    [_, ip, mask, gateway] = device.get_current_ip()

                    print(f"Found Aravis camera: {model_name} (SN: {serial}, IP: {ip.to_string()})\n")

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
                    print(f"Error reading Aravis camera info at index {idx}: {e}\n")

        except Exception as e:
            print(f"Error scanning Aravis cameras: {e}\n")

        return cameras

    @staticmethod
    def scan_all() -> List[Dict]:
        print("Scanning Aravis cameras...\n")
        arv_cams = CameraScanner.scan_aravis_cameras()
        print(f"Found {len(arv_cams)} Aravis cameras\n")
        return arv_cams


# ============ ФАБРИКА (только aravis) ============
def create_camera(camera_type: str = "aravis", **kwargs) -> CameraInterface:
    if camera_type in ("gige", "aravis"):
        saved_ip = kwargs.get("saved_ip", None)
        return ArvCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=saved_ip
        )
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")