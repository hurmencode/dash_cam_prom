import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import re
import time
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


# ============ ВИДЕОРЕКОРДЕР (GStreamer x264enc, fallback MJPG) ============
class VideoRecorder:
    """
    Рекордер на GStreamer + x264enc (программный кодек).
    GStreamer сам управляет PTS — нет segfault'ов при закрытии.
    Fallback на MJPG, если GStreamer не собран в OpenCV.
    """

    def __init__(self, output_dir: str = "recordings", fps: int = 30,
                 is_color: bool = False):
        self.output_dir = output_dir
        self.fps = max(1, int(fps))
        self.is_color = is_color
        self.writer = None
        self.is_recording = False
        self.record_path = None
        self.recording_start_time = None
        self.frame_count = 0
        self.codec = None

        os.makedirs(output_dir, exist_ok=True)

    def _build_gst_pipeline(self, width, height, path):
        fmt = "BGR" if self.is_color else "GRAY8"
        return (
            f"appsrc ! video/x-raw,format={fmt} ! "
            f"videoconvert ! video/x-raw,format=I420 ! "
            f"x264enc speed-preset=ultrafast tune=zerolatency "
            f"key-int-max=30 bitrate=5000 ! "
            f"h264parse ! mp4mux ! filesink location={path}"
        )

    def start_recording(self, width: int, height: int,
                        camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)

        # Пробуем GStreamer + x264enc
        mp4_path = os.path.join(self.output_dir,
                                f"{safe_name}_{timestamp}.mp4")
        pipeline = self._build_gst_pipeline(width, height, mp4_path)

        try:
            self.writer = cv2.VideoWriter(
                pipeline, cv2.CAP_GSTREAMER, 0, self.fps,
                (width, height), isColor=self.is_color
            )
            if self.writer.isOpened():
                self.record_path = mp4_path
                self.codec = "x264enc (GStreamer)"
            else:
                self.writer = None
        except Exception as e:
            print(f"GStreamer pipeline error: {e}\n")
            self.writer = None

        # Fallback на MJPG
        if self.writer is None:
            print("GStreamer недоступен, fallback на MJPG\n")
            avi_path = os.path.join(self.output_dir,
                                    f"{safe_name}_{timestamp}_mjpg.avi")
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            self.writer = cv2.VideoWriter(
                avi_path, fourcc, self.fps,
                (width, height), isColor=self.is_color
            )
            if self.writer.isOpened():
                self.record_path = avi_path
                self.codec = "MJPG"
            else:
                self.writer = None

        if self.writer is None or not self.writer.isOpened():
            raise RuntimeError("Не удалось создать VideoWriter (ни GStreamer, ни MJPG)\n")

        self.is_recording = True
        self.recording_start_time = time.time()
        self.frame_count = 0

        print(f"Запись начата: {self.record_path}\n")
        print(f"   Кодек: {self.codec}, "
              f"{width}x{height}, isColor={self.is_color}, fps={self.fps}\n")
        return self.record_path

    def write_frame(self, frame: np.ndarray) -> bool:
        if self.writer is None or not self.is_recording:
            return False
        if frame is None:
            return False
        try:
            self.writer.write(frame)
            self.frame_count += 1
            return True
        except Exception as e:
            print(f"Error writing frame: {e}\n")
            return False

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None

        self.is_recording = False

        # Даём GStreamer/OpenCV время дописать буфер
        time.sleep(0.3)

        if self.writer:
            self.writer.release()
            self.writer = None

            duration = time.time() - self.recording_start_time
            try:
                file_size = os.path.getsize(self.record_path) / (1024 * 1024)
            except OSError:
                file_size = 0.0

            print(f"Запись остановлена: {self.record_path}\n")
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
            'is_color': self.is_color
        }


# ============ ARV CAMERA MANAGER (ARAVIS API) ============
class ArvCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8',
                 device_index: int = 0, saved_ip: str = None):
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
            print(f"Connected Aravis: {model_name} "
                  f"(SN: {serial}, IP: {ip.to_string()})\n")

            if self.pixel_format:
                try:
                    self.camera.set_pixel_format_from_string(self.pixel_format)
                except Exception as e:
                    print(f"Could not set pixel format {self.pixel_format}: {e}\n")

            self.stream = self.camera.create_stream(None, None)
            payload = self.camera.get_payload()
            for _ in range(10):
                self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))

            self.camera.start_acquisition()

        except Exception as e:
            print(f"Error connecting to Aravis camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise

    def get_frame(self) -> Optional[np.ndarray]:
        """Возвращает Mono8 (2D numpy array). Без cvtColor."""
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