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
            mode: 'lossy' - XVID (сжатый)
                  'lossless' - FFV1 (без потерь)
                  'uncompressed' - MJPG (минимальное сжатие)
                  'png_frames' - каждый кадр как PNG
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
        
        # Выбираем кодек в зависимости от режима
        codecs = {
            'lossy': 'XVID',
            'lossless': 'FFV1',
            'uncompressed': 'MJPG',  # MJPG - почти без потерь, работает везде
            'png_frames': 'XVID'
        }
        self.fourcc = codecs.get(mode, 'XVID')
        
        # Расширение файла
        self.extensions = {
            'lossy': '.avi',
            'lossless': '.mkv',
            'uncompressed': '.avi',
            'png_frames': '.avi'
        }
        self.extension = self.extensions.get(mode, '.avi')
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Если режим PNG - создаем папку для кадров
        if self.save_frames_as_png:
            self.png_dir = os.path.join(output_dir, 'frames')
            os.makedirs(self.png_dir, exist_ok=True)
    
    def start_recording(self, width: int, height: int, camera_name: str = "camera") -> str:
        if self.is_recording:
            return self.record_path
        
        self.frame_width = width
        self.frame_height = height
        self.frame_count = 0
        self.saved_frames = []
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
        
        mode_labels = {
            'lossy': 'compressed',
            'lossless': 'lossless',
            'uncompressed': 'uncompressed',
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
        
        # Если не получилось - пробуем альтернативные кодеки
        if not self.writer.isOpened():
            print(f"⚠️ Кодек {self.fourcc} не поддерживается, пробуем альтернативы...")
            
            # Список альтернативных кодеков для каждого режима
            fallback_codecs = {
                'lossy': ['MJPG', 'X264', 'XVID'],
                'lossless': ['HFYU', 'FFV1', 'MJPG'],
                'uncompressed': ['MJPG', 'XVID', 'X264'],
                'png_frames': ['MJPG', 'XVID', 'X264']
            }
            
            for codec in fallback_codecs.get(self.mode, ['MJPG', 'XVID']):
                fourcc_code = cv2.VideoWriter_fourcc(*codec)
                self.writer = cv2.VideoWriter(
                    self.record_path,
                    fourcc_code,
                    self.fps,
                    (width, height)
                )
                if self.writer.isOpened():
                    self.fourcc = codec
                    print(f"   ✅ Используем кодек: {codec}")
                    break
        
        if not self.writer.isOpened():
            raise RuntimeError(f"Failed to create video writer: {self.record_path}")
        
        self.is_recording = True
        self.recording_start_time = time.time()
        
        mode_names = {
            'lossy': 'Сжатый',
            'lossless': 'Без потерь',
            'uncompressed': 'Минимальное сжатие',
            'png_frames': 'Кадры PNG'
        }
        print(f"🔴 Запись начата: {self.record_path}")
        print(f"   Режим: {mode_names.get(self.mode, self.mode)}, Кодек: {self.fourcc}")
        
        if self.save_frames_as_png:
            print(f"   📁 PNG кадры будут сохранены в: {self.png_dir}")
        
        return self.record_path
    
    def write_frame(self, frame: np.ndarray) -> bool:
        if not self.is_recording or self.writer is None:
            return False
        
        try:
            if frame.shape[1] != self.frame_width or frame.shape[0] != self.frame_height:
                frame = cv2.resize(frame, (self.frame_width, self.frame_height))
            
            self.writer.write(frame)
            self.frame_count += 1
            
            if self.save_frames_as_png:
                png_filename = f"frame_{self.frame_count:06d}.png"
                png_path = os.path.join(self.png_dir, png_filename)
                cv2.imwrite(png_path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 0])
                self.saved_frames.append(png_path)
            
            return True
        except Exception as e:
            print(f"Error writing frame: {e}")
            return False
    
    def stop_recording(self) -> Optional[str]:
        if not self.is_recording:
            return None
        
        self.is_recording = False
        
        if self.writer:
            self.writer.release()
            self.writer = None
            
            duration = time.time() - self.recording_start_time
            file_size = os.path.getsize(self.record_path) / (1024*1024)
            
            print(f"⏹️ Запись остановлена: {self.record_path}")
            print(f"   Кадров: {self.frame_count}, Длительность: {duration:.1f} сек")
            print(f"   Размер видео: {file_size:.1f} MB")
            
            if self.save_frames_as_png:
                png_size = sum(os.path.getsize(f) for f in self.saved_frames) / (1024*1024)
                print(f"   PNG кадров: {len(self.saved_frames)}, Размер: {png_size:.1f} MB")
            
            return self.record_path
        
        return None
    
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
            raise RuntimeError(f"Cannot open webcam {device_id}")
    
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
        """Получает IP-адрес камеры"""
        try:
            # Если IP уже сохранен - возвращаем его
            if self.saved_ip and self.saved_ip != 'Unknown':
                return self.saved_ip
            
            # Пробуем получить IP из устройства
            ip_methods = ['DeviceIPAddress', 'GevCurrentIPAddress', 'GevPersistentIPAddress']
            
            for node_name in ip_methods:
                try:
                    node = self.camera.nodemap.get_node(node_name)
                    if node:
                        value = node.value
                        
                        # Если это число - конвертируем в IP
                        if isinstance(value, int):
                            ip_bytes = value.to_bytes(4, byteorder='big')
                            return '.'.join(str(b) for b in ip_bytes)
                        # Если это строка
                        elif isinstance(value, str):
                            return value
                except:
                    continue
            
            # Пробуем получить через интерфейс
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
            print(f"Error getting camera IP: {e}")
            return 'Unknown'
    
    def _connect_camera(self):
        try:
            devices = self.system.create_device()
            
            if not devices:
                raise RuntimeError("No Lucid Triton cameras found")
            
            if self.device_index < len(devices):
                self.camera = devices[self.device_index]
            else:
                self.camera = devices[0]
            
            try:
                model_name = self.camera.nodemap.get_node('DeviceModelName').value
                serial = self.camera.nodemap.get_node('DeviceSerialNumber').value
                
                # Получаем IP (используем сохраненный если есть)
                ip_address = self._get_camera_ip()
                
                self._info = {
                    'name': model_name,
                    'serial': serial,
                    'ip': ip_address,
                    'status': 'Connected'
                }
                print(f"Connected: {model_name} (SN: {serial}, IP: {ip_address})")
                
            except Exception as e:
                print(f"Error reading camera info: {e}")
                self._info['status'] = 'Connected (Info Error)'
            
            if self.pixel_format:
                try:
                    self.camera.nodemap.get_node('PixelFormat').value = self.pixel_format
                except:
                    pass
            
            self.camera.start_stream()
            
        except Exception as e:
            print(f"Error connecting to Lucid camera: {e}")
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
            print(f"Frame conversion error: {e}")
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
    """Сканер для поиска всех доступных камер"""
    
    @staticmethod
    def _extract_ip_from_device(device) -> str:
        """Извлекает IP-адрес из устройства Lucid"""
        try:
            # Метод 1: DeviceIPAddress
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
            
            # Метод 2: GevCurrentIPAddress
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
            
            # Метод 3: Через интерфейс
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
        """Сканирует USB веб-камеры"""
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
        """Сканирует Lucid Triton камеры и сохраняет IP-адреса"""
        cameras = []
        
        try:
            from arena_api.system import system
            
            devices = system.create_device()
            
            if not devices:
                print("No Lucid devices found")
                return cameras
            
            for idx, device in enumerate(devices):
                try:
                    # Получаем информацию о камере
                    model_name = device.nodemap.get_node('DeviceModelName').value
                    serial = device.nodemap.get_node('DeviceSerialNumber').value
                    
                    # Извлекаем IP-адрес
                    ip_address = CameraScanner._extract_ip_from_device(device)
                    
                    print(f"Found Lucid camera: {model_name} (SN: {serial}, IP: {ip_address})")
                    
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
                    print(f"Error reading Lucid camera info: {e}")
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
            
            # Освобождаем устройства
            system.destroy_device()
            
        except ImportError:
            print("Arena SDK not installed")
        except Exception as e:
            print(f"Error scanning Lucid cameras: {e}")
        
        return cameras
    
    @staticmethod
    def scan_all() -> List[Dict]:
        """Сканирует все типы камер"""
        all_cameras = []
        
        print("Scanning webcams...")
        webcams = CameraScanner.scan_webcams()
        all_cameras.extend(webcams)
        print(f"Found {len(webcams)} webcams")
        
        print("Scanning Lucid cameras...")
        lucid_cams = CameraScanner.scan_lucid_cameras()
        all_cameras.extend(lucid_cams)
        print(f"Found {len(lucid_cams)} Lucid cameras")
        
        return all_cameras

# ============ ФАБРИКА СОЗДАНИЯ КАМЕР ============
def create_camera(camera_type: str = "webcam", **kwargs) -> CameraInterface:
    if camera_type == "webcam":
        return WebcamManager(device_id=kwargs.get("device_id", 0))
    elif camera_type == "lucid":
        # Передаем сохраненный IP в конструктор
        saved_ip = kwargs.get("saved_ip", None)
        return LucidCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=saved_ip
        )
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")