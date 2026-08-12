import os
import cv2
import numpy as np
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
import subprocess
import platform
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
        """
        Args:
            mode: 
                'lossy' - XVID (сжатый с потерями, маленький размер)
                'lossless' - HFYU (без потерь, идеальное качество)
                'high_quality' - MJPG (минимальные потери, большой размер)
                'png_frames' - MJPG + PNG кадры (для нейросетей)
        """
        self.output_dir = output_dir
        self.fps = fps
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
        #self._frame_interval = 1.0 / self.fps
        
        # ============ РАБОЧИЕ КОДЕКИ ============
        codecs = {
            'lossy': 'XVID',        # Сжатый с потерями
            'lossless': 'HFYU',     # Без потерь
            'high_quality': 'MJPG', # Высокое качество (минимальные потери)
            'png_frames': 'MJPG'    # Видео + PNG
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
            'lossless': 'lossless_hfyu',
            'high_quality': 'high_quality_mjpg',
            'png_frames': 'png_frames'
        }
        mode_label = mode_labels.get(self.mode, 'video')
        filename = f"{safe_name}_{timestamp}_{mode_label}{self.extension}"
        self.record_path = os.path.join(self.output_dir, filename)
        
        # Пробуем создать VideoWriter с выбранным кодеком
        fourcc_code = cv2.VideoWriter_fourcc(*self.fourcc)
        self.writer = cv2.VideoWriter(
            self.record_path,
            fourcc_code,
            self.fps,
            (width, height)
        )
        
        # Fallback если кодек не поддерживается
        if not self.writer.isOpened():
            print(f" Кодек {self.fourcc} не поддерживается, пробуем альтернативы...\n")
            
            fallback_codecs = {
                'lossy': ['XVID', 'X264'],
                'lossless': ['HFYU', 'FFV1'],
                'high_quality': ['MJPG'],
                'png_frames': ['MJPG']
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
                    print(f"    Используем кодек: {codec}\n")
                    break
        
        if not self.writer.isOpened():
            raise RuntimeError(f"Failed to create video writer: {self.record_path}\n")
        
        self.is_recording = True
        self.recording_start_time = time.time()
        
        mode_names = {
            'lossy': 'Сжатый (XVID)',
            'lossless': 'Без потерь (HFYU)',
            'high_quality': 'Минимальное сжатие (MJPG)',
            'png_frames': 'Кадры PNG'
        }
        print(f" Запись начата: {self.record_path}\n")
        print(f"   Режим: {mode_names.get(self.mode, self.mode)}\n")
        print(f"   Кодек: {self.fourcc}, FPS: {self.fps}\n")
        
        if self.save_frames_as_png:
            print(f"    PNG кадры будут сохранены в: {self.png_dir}\n")
        
        return self.record_path
    
    def write_frame(self, frame: np.ndarray) -> bool:
        if not self.is_recording or self.writer is None:
            return False
        
        current_time = time.time()
        # if current_time - self._last_frame_time < self._frame_interval:
        #     return True
        
        try:
            # ============ ПРОВЕРКА ФОРМАТА КАДРА ============
            # Проверяем что кадр существует
            if frame is None:
                return False
            
            # Проверяем тип данных
            if frame.dtype != np.uint8:
                try:
                    frame = frame.astype(np.uint8)
                except:
                    return False
            
            # Проверяем размерность
            if len(frame.shape) == 2:
                # Если серый, конвертируем в BGR
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            elif len(frame.shape) == 3:
                if frame.shape[2] == 4:
                    # RGBA -> BGR
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
                elif frame.shape[2] == 3:
                    # Уже BGR или RGB
                    pass
                else:
                    return False
            else:
                return False
            # ================================================
            
            # Изменяем размер если нужно
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
        
        if self.save_frames_as_png and self.png_writer_thread:
            self.png_writer_running = False
            self.png_queue.put(None)
            self.png_writer_thread.join(timeout=5)
        
        if self.writer:
            self.writer.release()
            self.writer = None
            
            duration = time.time() - self.recording_start_time
            file_size = os.path.getsize(self.record_path) / (1024*1024)
            
            print(f" Запись остановлена: {self.record_path}\n")
            print(f"   Кадров: {self.frame_count}\n")
            print(f"   Длительность: {duration:.1f} сек\n")
            print(f"   Размер видео: {file_size:.1f} MB\n")
            
            if self.save_frames_as_png:
                print(f"   PNG кадров: {len(self.saved_frames)}\n")
            
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
            
            if frame_count % 100 == 0:
                print(f"   Извлечено кадров: {frame_count}\n")
        
        cap.release()
        print(f" Извлечено {frame_count} кадров в {output_dir}\n")
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
                print(f"Ошибка записи PNG: {e}\n")

# ============ WEBCAM MANAGER ============
class WebcamManager(CameraInterface):
    def __init__(self, device_id: int = 0):
        self.device_id = device_id
        
        if os.name == 'nt':
            self.cap = cv2.VideoCapture(device_id, cv2.CAP_DSHOW)
        else:
            self.cap = cv2.VideoCapture(device_id)
            
        self._info = {
            'name': f'Webcam {device_id}',
            'serial': f'USB-{device_id}',
            'ip': 'N/A',
            'status': 'Connected' if self.cap and self.cap.isOpened() else 'Failed'
        }
        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"Cannot open webcam {device_id}\n")
    
    def get_frame(self) -> Optional[np.ndarray]:
        if self.cap:
            ret, frame = self.cap.read()
            return frame if ret else None
        return None
    
    def release(self):
        if self.cap:
            self.cap.release()
    
    def get_info(self) -> Dict:
        return self._info

# ============ LUCID CAMERA MANAGER (ARENA API) ============
class LucidCameraManager(CameraInterface):
    def __init__(self, pixel_format: str = 'Mono8', device_index: int = 0, saved_ip: str = None):
        from arena_api.system import system
        self.system = system
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
            
            try:
                if hasattr(self.camera, 'tl_device'):
                    interface = str(self.camera.tl_device.interface)
                    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', interface)
                    if ip_match:
                        return ip_match.group(1)
            except:
                pass
            
            return 'Unknown'
        except Exception as e:
            print(f"Error getting camera IP: {e}\n")
            return 'Unknown'
    
    def _connect_camera(self):
        try:
            devices = self.system.create_device()
            
            if not devices:
                raise RuntimeError("No Lucid Triton cameras found\n")
            
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
                print(f"Connected: {model_name} (SN: {serial}, IP: {ip_address})\n")
                
            except Exception as e:
                print(f"Error reading camera info: {e}\n")
                self._info['status'] = 'Connected (Info Error)'
            
            if self.pixel_format:
                try:
                    self.camera.nodemap.get_node('PixelFormat').value = self.pixel_format
                except:
                    pass
            
            self.camera.start_stream()
            
        except Exception as e:
            print(f"Error connecting to Lucid camera: {e}\n")
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
            print(f"Frame conversion error: {e}\n")
            frame = None
        finally:
            self.camera.requeue_buffer(buffer)
        
        return frame
    
    def release(self):
        if self.camera:
            try:
                self.camera.stop_stream()
                self.system.destroy_device()
            except:
                pass
    
    def get_info(self) -> Dict:
        return self._info

# ============ СКАНЕР КАМЕР ============
class CameraScanner:
    @staticmethod
    def _extract_ip_from_device(device) -> str:
        try:
            try:
                node = device.nodemap.get_node('DeviceIPAddress')
                if node:
                    value = node.value
                    if isinstance(value, int):
                        ip_bytes = value.to_bytes(4, byteorder='big')
                        return '.'.join(str(b) for b in ip_bytes)
                    elif isinstance(value, str):
                        return value
            except:
                pass
            
            try:
                node = device.nodemap.get_node('GevCurrentIPAddress')
                if node:
                    value = node.value
                    if isinstance(value, int):
                        ip_bytes = value.to_bytes(4, byteorder='big')
                        return '.'.join(str(b) for b in ip_bytes)
                    elif isinstance(value, str):
                        return value
            except:
                pass
            
            try:
                if hasattr(device, 'tl_device'):
                    interface = str(device.tl_device.interface)
                    ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', interface)
                    if ip_match:
                        return ip_match.group(1)
            except:
                pass
            
            return 'Unknown'
        except:
            return 'Unknown'
    
    @staticmethod
    def scan_webcams(max_devices: int = 10) -> List[Dict]:
        cameras = []
        for device_id in range(max_devices):
            try:
                if os.name == 'nt':
                    cap = cv2.VideoCapture(device_id, cv2.CAP_DSHOW)
                else:
                    cap = cv2.VideoCapture(device_id)
                
                if cap and cap.isOpened():
                    ret, frame = cap.read()
                    if ret:
                        cameras.append({
                            'name': f'Webcam {device_id}',
                            'serial': f'USB-{device_id}',
                            'ip': 'N/A',
                            'status': 'Available',
                            'type': 'webcam',
                            'device_id': device_id
                        })
                    cap.release()
            except Exception:
                continue
        return cameras
    
    @staticmethod
    def scan_lucid_cameras() -> List[Dict]:
        cameras = []
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
                    cameras.append({
                        'name': f'Lucid Camera {idx}',
                        'serial': 'Unknown',
                        'ip': 'Unknown',
                        'status': 'Available',
                        'type': 'lucid',
                        'device_id': idx,
                        'device': device,
                        '_saved_ip': 'Unknown'
                    })
            
            system.destroy_device()
            
        except ImportError:
            print("Arena SDK not installed\n")
        except Exception as e:
            print(f"Error scanning Lucid cameras: {e}\n")
        
        return cameras
    
    @staticmethod
    def scan_all() -> List[Dict]:
        all_cameras = []
        
        print("Scanning webcams...\n")
        webcams = CameraScanner.scan_webcams()
        all_cameras.extend(webcams)
        print(f"Found {len(webcams)} webcams\n")
        
        print("Scanning Lucid cameras...\n")
        lucid_cams = CameraScanner.scan_lucid_cameras()
        all_cameras.extend(lucid_cams)
        print(f"Found {len(lucid_cams)} Lucid cameras\n")
        
        return all_cameras

# ============ ФАБРИКА ============
def create_camera(camera_type: str = "webcam", **kwargs) -> CameraInterface:
    if camera_type == "webcam":
        return WebcamManager(device_id=kwargs.get("device_id", 0))
    elif camera_type == "lucid":
        saved_ip = kwargs.get("saved_ip", None)
        return LucidCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=saved_ip
        )
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")