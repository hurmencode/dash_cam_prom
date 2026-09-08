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
            
            # Выбираем ID устройства по индексу
            idx = self.device_index if self.device_index < n_devices else 0
            device_id = Aravis.get_device_id(idx)
            
            # Создаем объект камеры
            self.camera = Aravis.Camera.new(device_id)
            
            # Собираем метаданные через стандартные функции Aravis
            model_name = self.camera.get_model_name()
            serial = self.camera.get_device_serial_number()
            
            # Извлекаем IP
            device = self.camera.get_device()
            [_, ip, mask, gateway] = device.get_current_ip()
            
            self._info = {
                'name': model_name,
                'serial': serial,
                'ip': ip.to_string(),
                'status': 'Connected'
            }
            print(f"Connected Aravis: {model_name} (SN: {serial}, IP: {ip.to_string()})\n")
            
            # Установка формата пикселей
            if self.pixel_format:
                try:
                    self.camera.set_pixel_format_from_string(self.pixel_format)
                except Exception as e:
                    print(f"Could not set pixel format {self.pixel_format}: {e}")
            
            # Настройка потока и выделение буферов
            self.stream = self.camera.create_stream(None, None)
            payload = self.camera.get_payload()
            for _ in range(5):
                self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))
            
            # Запуск трансляции
            self.camera.start_acquisition()
            
        except Exception as e:
            print(f"Error connecting to Aravis camera: {e}\n")
            self._info['status'] = f'Error: {str(e)[:50]}'
            raise
    
    def get_frame(self) -> Optional[np.ndarray]:
        if not self.stream:
            return None
            
        # Запрашиваем буфер (таймаут 1 секунда = 1 000 000 мкс)
        buffer = self.stream.timeout_pop_buffer(1000000)
        if buffer is None:
            return None
        
        frame = None
        try:
            if buffer.get_status() == Aravis.BufferStatus.SUCCESS:
                # Извлекаем сырые данные
                data = buffer.get_data() 
                
                # Получаем геометрию кадра из буфера
                try:
                    width = buffer.get_image_width()
                    height = buffer.get_image_height()
                except AttributeError:
                    # Для более новых версий API Aravis:
                    _, _, width, height = buffer.get_image_region()
                
                # Преобразуем данные в numpy array
                img_array = np.frombuffer(data, dtype=np.uint8).reshape((height, width))
                frame = cv2.cvtColor(img_array, cv2.COLOR_GRAY2BGR)
        except Exception as e:
            print(f"Frame conversion error: {e}\n")
            frame = None
        finally:
            # Обязательно возвращаем буфер обратно в поток очереди Aravis
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
                print(f"Error releasing Aravis camera: {e}")
    
    def get_info(self) -> Dict:
        return self._info

# ============ СКАНЕР КАМЕР ============
class CameraScanner:
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
    def scan_aravis_cameras() -> List[Dict]:
        cameras = []
        try:
            # Обновляем список устройств в Aravis
            Aravis.update_device_list()
            n_devices = Aravis.get_n_devices()
            
            if n_devices == 0:
                print("No Aravis devices found\n")
                return cameras
            
            for idx in range(n_devices):
                try:
                    # Получаем уникальный строковый ID устройства 
                    device_id = Aravis.get_device_id(idx)
                    camera = Aravis.Camera.new(device_id)
                    
                    # Получение модели, вендора и серийника в Aravis API
                    model_name = Aravis.get_device_model(idx) if hasattr(Aravis, 'get_device_model') else "GenICam Camera"
                    
                    # Извлекаем серийный номер (если метод доступен глобально, иначе ставим N/A)
                    serial = camera.get_device_serial_number()
                    
                    # Извлекаем IP-адрес
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
        all_cameras = []
        
        print("Scanning webcams...\n")
        webcams = CameraScanner.scan_webcams()
        all_cameras.extend(webcams)
        print(f"Found {len(webcams)} webcams\n")
        
        print("Scanning Aravis cameras...\n")
        arv_cams = CameraScanner.scan_aravis_cameras() # Вызов нового сканера вместо scan_lucid_cameras
        all_cameras.extend(arv_cams)
        print(f"Found {len(arv_cams)} Aravis cameras\n")
        
        return all_cameras

# ============ ФАБРИКА ============
def create_camera(camera_type: str = "webcam", **kwargs) -> CameraInterface:
    if camera_type == "webcam":
        return WebcamManager(device_id=kwargs.get("device_id", 0))
    elif camera_type in ("gige", "aravis"):
        saved_ip = kwargs.get("saved_ip", None)
        return ArvCameraManager(
            pixel_format=kwargs.get("pixel_format", "Mono8"),
            device_index=kwargs.get("device_id", 0),
            saved_ip=saved_ip
        )
    else:
        raise ValueError(f"Unknown camera type: {camera_type}")