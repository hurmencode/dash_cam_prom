import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import re
import time
from datetime import datetime
import threading
import queue


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


# ============ ВИДЕОРЕКОРДЕР ============
class VideoRecorder:
    def __init__(self, output_dir: str = "recordings", fps: int = 30,
                 mode: str = 'lossy'):
        self.output_dir = output_dir
        # Защита от FPS = 0, из-за которого падал OpenCV/FFmpeg (Segmentation fault)
        self.fps = max(1, int(fps)) if fps is not None else 30
        self.mode = mode
        self.writer = None
        self.is_recording = False
        self.record_path = None
        self.frame_width = None
        self.frame_height = None
        self.recording_start_time = None
        self.frame_count = 0
        self.saved_frames = []
        self.save_frames_as_png = (mode == 'png_frames')
        self._last_frame_time = 0

        codecs = {
            'lossy': 'XVID',
            'lossless': 'MJPG',       # HFYU на Jetson валит систему
            'high_quality': 'MJPG',
            'png_frames': 'MJPG'
        }
        self.fourcc = codecs.get(mode, 'XVID')

        extensions = {
            'lossy': '.avi',
            'lossless': '.avi',
            'high_quality': '.avi',
            'png_frames': '.avi'
        }
        self.extension = extensions.get(mode, '.avi')

        os.makedirs(output_dir, exist_ok=True)

        if self.save_frames_as_png:
            self.png_dir = os.path.join(output_dir, 'frames')
            os.makedirs(self.png_dir, exist_ok=True)
            self.png_queue = queue.Queue(maxsize=200)
            self.png_writer_thread = None
            self.png_writer_running = False
            self._start_png_writer()

    def start_recording(self, width: int, height: int, camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path

        self.frame_width = width
        self.frame_height = height
        self.frame_count = 0
        self.saved_frames = []
        self._last_frame_time = 0

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)

        mode_labels = {
            'lossy': 'compressed',
            'lossless': 'lossless_mjpg',
            'high_quality': 'high_quality_mjpg',
            'png_frames': 'png_frames'
        }
        mode_label = mode_labels.get(self.mode, 'video')
        filename = f"{safe_name}_{timestamp}_{mode_label}{self.extension}"
        self.record_path = os.path.join(self.output_dir, filename)

        fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc)
        self.writer = cv2.VideoWriter(
            self.record_path,
            fourcc_code,
            self.fps,
            (width, height)
        )

        if not self.writer.isOpened():
            print(f" Codec {self.fourcc} not supported, trying alternatives...\n")
            fallback_codecs = {
                'lossy': ['XVID', 'MJPG'],
                'lossless': ['MJPG', 'XVID'],
                'high_quality': ['MJPG', 'XVID'],
                'png_frames': ['MJPG', 'XVID']
            }

            for codec in fallback_codecs.get(self.mode, ['XVID', 'MJPG']):
                fourcc_code = cv2.VideoWriter_fourcc(*codec)
                self.writer = cv2.VideoWriter(
                    self.record_path,
                    fourcc_code,
                    self.fps,
                    (width, height)
                )
                if self.writer.isOpened():
                    self.fourcc = codec
                    print(f"    Using codec: {codec}\n")
                    break

        if not self.writer.isOpened():
            raise RuntimeError(f"Failed to create video writer: {self.record_path}\n")

        self.is_recording = True
        self.recording_start_time = time.time()

        mode_names = {
            'lossy': 'Compressed (XVID)',
            'lossless': 'Lossless (MJPG)',
            'high_quality': 'High quality (MJPG)',
            'png_frames': 'PNG frames'
        }
        print(f" Recording started: {self.record_path}\n")
        print(f"   Mode: {mode_names.get(self.mode, self.mode)}\n")
        print(f"   Codec: {self.fourcc}, FPS: {self.fps}\n")

        if self.save_frames_as_png:
            print(f"   PNG frames will be saved to: {self.png_dir}\n")

        return self.record_path

    def write_frame(self, frame: np.ndarray) -> bool:
        if not self.is_recording or self.writer is None:
            return False

        # Защита от None-кадра
        if frame is None:
            return False

        current_time = time.time()
        try:
            if frame.dtype != np.uint8:
                try:
                    frame = frame.astype(np.uint8)
                except:
                    return False

            if len(frame.shape) == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif len(frame.shape) == 3:
                if frame.shape[2] == 4:
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
            else:
                return False

            if frame.shape[1] != self.frame_width or frame.shape[0] != self.frame_height:
                frame = cv2.resize(frame, (self.frame_width, self.frame_height))

            self.writer.write(frame)
            self.frame_count += 1
            self._last_frame_time = current_time

            if self.save_frames_as_png:
                try:
                    frame_copy = frame.copy()
                    self.png_queue.put_nowait((frame_copy, self.frame_count))
                except queue.Full:
                    pass

            return True
        except Exception as e:
            print(f"Error writing frame: {e}")
            return False

    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None

        self.is_recording = False
        time.sleep(0.3)

        if self.save_frames_as_png and self.png_writer_thread:
            self.png_writer_running = False
            self.png_queue.put(None)
            self.png_writer_thread.join(timeout=5)

        if self.writer:
            self.writer.release()
            self.writer = None

            duration = time.time() - self.recording_start_time
            try:
                file_size = os.path.getsize(self.record_path) / (1024 * 1024)
            except:
                file_size = 0

            print(f" Recording stopped: {self.record_path}\n")
            print(f"   Frames: {self.frame_count}\n")
            print(f"   Duration: {duration:.1f} sec\n")
            print(f"   File size: {file_size:.1f} MB\n")

            return self.record_path
        return None

    def extract_frames_to_png(self, video_path: str, output_dir: str = None) -> List[str]:
        if output_dir is None:
            output_dir = os.path.join(os.path.dirname(video_path), 'frames_extracted')

        os.makedirs(output_dir, exist_ok=True)
        cap = cv2.VideoCapture(video_path)
        frame_count = 0
        saved_paths = []

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            png_path = os.path.join(output_dir, f"frame_{frame_count:06d}.png")
            cv2.imwrite(png_path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 0])
            saved_paths.append(png_path)
            frame_count += 1

        cap.release()
        return saved_paths

    def get_recording_status(self) -> Dict:
        return {
            'is_recording': self.is_recording,
            'file_path': self.record_path,
            'duration': time.time() - self.recording_start_time if self.recording_start_time else 0,
            'fps': self.fps,
            'frame_count': self.frame_count,
            'mode': self.mode,
            'fourcc': self.fourcc,
            'save_frames_as_png': self.save_frames_as_png
        }

    def _start_png_writer(self):
        self.png_writer_running = True
        self.png_writer_thread = threading.Thread(target=self._png_writer_loop, daemon=True)
        self.png_writer_thread.start()

    def _png_writer_loop(self):
        while self.png_writer_running:
            try:
                item = self.png_queue.get(timeout=0.1)
                if item is None:
                    break
                frame, frame_number = item
                png_path = os.path.join(self.png_dir, f"frame_{frame_number:06d}.png")
                cv2.imwrite(png_path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 0])
                self.saved_frames.append(png_path)
                self.png_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"PNG write error: {e}\n")


# ============ LUCID CAMERA MANAGER (ARENA API) ============
class LucidCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8', device_index: int = 0, saved_ip: str = None):
        try:
            from arena_api.system import system
            self.system = system
        except ImportError:
            raise RuntimeError("Arena SDK is not installed. Make sure arena_api library is available.")

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

            ip_methods = ['DeviceIPAddress', 'GevCurrentIPAddress', 'GevPersistentIPAddress']

            for node_name in ip_methods:
                try:
                    node = self.camera.nodemap.get_node(node_name)
                    if node:
                        value = node.value
                        if isinstance(value, int):
                            ip_bytes = value.to_bytes(4, byteorder='big')
                            return '.'.join(str(b) for b in ip_bytes)
                        elif isinstance(value, str):
                            return value
                except:
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
                model_name = self.camera.nodemap.get_node('DeviceModelName').value
                serial = self.camera.nodemap.get_node('DeviceSerialNumber').value
                ip_address = self._get_camera_ip()

                self._info = {
                    'name': model_name,
                    'serial': serial,
                    'ip': ip_address,
                    'status': 'Connected'
                }
                print(f"[SUCCESS] Connected: {model_name} (SN: {serial}, IP: {ip_address})\n")

            except Exception as e:
                print(f"[WARN] Error reading camera info: {e}\n")
                self._info['status'] = 'Connected (Info Error)'

            if self.pixel_format:
                try:
                    self.camera.nodemap.get_node('PixelFormat').value = self.pixel_format
                except Exception as e:
                    print(f"[WARN] Failed to set pixel format {self.pixel_format}: {e}")

            self.camera.start_stream()

        except Exception as e:
            print(f"[ERROR] Error connecting to Lucid camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise

    def get_frame(self) -> Optional[np.ndarray]:
        if not self.camera:
            return None

        buffer = self.camera.get_buffer()
        if buffer is None:
            return None

        try:
            height, width = buffer.height, buffer.width
            img_array = np.ctypeslib.as_array(buffer.pdata, shape=(height, width))
            frame = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)
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
            for node_name in ['DeviceIPAddress', 'GevCurrentIPAddress']:
                try:
                    node = device.nodemap.get_node(node_name)
                    if node:
                        value = node.value
                        if isinstance(value, int):
                            ip_bytes = value.to_bytes(4, byteorder='big')
                            return '.'.join(str(b) for b in ip_bytes)
                        elif isinstance(value, str):
                            return value
                except:
                    pass
            return 'Unknown'
        except:
            return 'Unknown'

    @staticmethod
    def scan_lucid_cameras() -> list:
        """Scan network for connected Lucid Triton GigE cameras (with duplicate filter)"""
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
                    model_name = device.nodemap.get_node('DeviceModelName').value
                    serial = device.nodemap.get_node('DeviceSerialNumber').value
                    ip_address = CameraScanner._extract_ip_from_device(device)

                    # Пропускаем дубликаты по серийному номеру
                    if serial in seen_serials:
                        print(f"[INFO] Skipping duplicate camera: {serial}\n")
                        continue
                    seen_serials.add(serial)

                    print(f"Found Lucid camera: {model_name} (SN: {serial}, IP: {ip_address})\n")
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
        """Entry point for global scan (returns only GigE cameras)"""
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
        raise ValueError(f"Unknown camera type: {camera_type}. USB cameras are no longer supported.")