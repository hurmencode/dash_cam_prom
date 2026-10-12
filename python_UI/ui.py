import re
import cv2
from datetime import datetime
import os
from PIL import Image, ImageTk
import platform
import subprocess
import sys
import time
import signal
import threading
import traceback
import tkinter as tk
from tkinter import PhotoImage, ttk, messagebox, filedialog
from threading import Thread, Lock

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from backend.camera_manager import (
    CameraScanner, create_camera,
    VideoRecorder, SegmentingRecorder
)


MIN_FREE_MB = 5000
SEGMENT_DURATION_SEC = 3600


class SlotState:
    def __init__(self, name: str):
        self.name = name
        self.camera = None
        self.camera_info = None
        self.recorder = None
        self.thread = None
        self.is_streaming = False
        self.display_enabled = False
        self.frame_bgr = None
        self.frame_ready = False
        self.frame_lock = Lock()
        self.current_fps = 0.0
        self.frame_count = 0
        self.fps_start_time = time.time()
        self.frame_size = None
        self.is_recording = False
        self.record_dir = None
        self.segment_paths = []
        self.recording_start_time = None
        self.canvas_image_id = None
        self.label = None
        self.canvas = None


class CameraDiscoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Видеорегистратор 3000")
        self._closing = False

        try:
            icon = PhotoImage(file="favicon.png")
            self.root.iconphoto(False, icon)
        except Exception as e:
            print(f"Не удалось загрузить иконку: {e}")

        self.root.geometry("1400x800")

        log_dir = os.path.join(os.path.expanduser("~"),
                               "dash_cam_prom", "logs")
        os.makedirs(log_dir, exist_ok=True)
        session_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log_file_path = os.path.join(
            log_dir, f"session_{session_ts}.log")
        try:
            self.log_file = open(self.log_file_path, 'a', encoding='utf-8')
        except Exception as e:
            print(f"Не удалось открыть файл логов: {e}")
            self.log_file = None

        self.slots = {
            'gige': SlotState('gige'),
            'usb': SlotState('usb'),
        }
        self.cameras = []
        self.is_recording = False

        self.create_widgets()
        self.install_crash_handlers()

        self.root.after(500, self.scan_cameras)
        self.root.after(30, self.update_ui_loop)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.log_info(f"Сессия логов: {self.log_file_path}")

    def install_crash_handlers(self):
        original_hook = sys.excepthook

        def excepthook(exc_type, exc_value, exc_tb):
            try:
                self.log_error(
                    f"НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ: "
                    f"{exc_type.__name__}: {exc_value}")
                tb = ''.join(traceback.format_exception(
                    exc_type, exc_value, exc_tb))
                self._log_to_file(tb)
            except Exception:
                pass
            finally:
                self._close_log_file()
                original_hook(exc_type, exc_value, exc_tb)

        sys.excepthook = excepthook

        def thread_excepthook(args):
            try:
                self.log_error(
                    f"ИСКЛЮЧЕНИЕ В ПОТОКЕ {args.thread.name}: "
                    f"{args.exc_type.__name__}: {args.exc_value}")
                tb = ''.join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback))
                self._log_to_file(tb)
            except Exception:
                pass

        threading.excepthook = thread_excepthook

        def signal_handler(signum, frame):
            try:
                self.log_warning(
                    f"Получен сигнал {signum}, завершаю работу...")
            except Exception:
                pass
            try:
                self.root.after(0, self.on_closing)
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except Exception as e:
            print(f"Не удалось установить обработчики сигналов: {e}")

    def _log_to_file(self, message):
        if self.log_file is not None:
            try:
                self.log_file.write(message + "\n")
                self.log_file.flush()
            except Exception:
                pass

    def _close_log_file(self):
        if getattr(self, 'log_file', None) is not None:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None

    def on_closing(self):
        if self._closing:
            return
        self._closing = True
        for slot in self.slots.values():
            slot.is_streaming = False
            slot.display_enabled = False
        try:
            if self.is_recording:
                self.stop_recording()
        except Exception:
            pass
        for slot in self.slots.values():
            try:
                if slot.camera:
                    slot.camera.release()
                    slot.camera = None
            except Exception:
                pass
        try:
            if self.log_file is not None:
                self.log_info("Приложение завершает работу, "
                              "логи сохранены.")
                self._close_log_file()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.camera_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.camera_tab, text="Камеры")
        self.create_camera_tab()

        self.video_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.video_tab, text="Видео")
        self.create_video_tab()

        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.log_tab, text="Логи")
        self.create_log_tab()

    def create_camera_tab(self):
        top_frame = ttk.Frame(self.camera_tab, padding="10")
        top_frame.pack(fill=tk.X)

        self.scan_btn = ttk.Button(
            top_frame, text="Поиск камер",
            command=self.scan_cameras, width=20)
        self.scan_btn.pack(side=tk.LEFT, padx=5)

        self.connect_btn = ttk.Button(
            top_frame, text="Подключиться",
            command=self.connect_camera, width=20, state=tk.DISABLED)
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.refresh_btn = ttk.Button(
            top_frame, text="Обновить статус",
            command=self.refresh_status, width=20)
        self.refresh_btn.pack(side=tk.LEFT, padx=5)

        self.loading_label = ttk.Label(top_frame, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)

        hint = ttk.Label(
            self.camera_tab,
            text="Выберите одну или две камеры "
                 "(Ctrl+Click для нескольких), затем 'Подключиться'.\n"
                 "Видео начнёт показываться автоматически.",
            foreground="#666666", justify=tk.LEFT)
        hint.pack(anchor=tk.W, padx=10, pady=(0, 5))

        columns = ('name', 'type', 'serial', 'ip', 'status')
        self.tree = ttk.Treeview(
            self.camera_tab, columns=columns,
            show='headings', height=15,
            selectmode='extended')

        self.status_bar = ttk.Label(
            self.camera_tab,
            text="Готов к работе. Нажмите 'Поиск камер'",
            relief=tk.SUNKEN)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        for col, text in zip(
            columns,
            ['Имя камеры', 'Тип', 'Серийный номер',
             'IP-адрес', 'Статус']
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, anchor='center')

        scrollbar = ttk.Scrollbar(self.camera_tab, orient=tk.VERTICAL,
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                       padx=(10, 0), pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y,
                       padx=(0, 10), pady=10)

        self.tree.bind('<<TreeviewSelect>>', self.on_select_camera)

    def create_video_tab(self):
        controls_frame = ttk.Frame(self.video_tab, padding="10")
        controls_frame.pack(fill=tk.X)

        self.video_info_label = ttk.Label(
            controls_frame, text="Камеры не подключены",
            font=('Arial', 10))
        self.video_info_label.pack(side=tk.LEFT, padx=5)

        self.fps_label_gige = ttk.Label(
            controls_frame, text="GigE FPS: —",
            font=('Arial', 10, 'bold'), foreground="#000000")
        self.fps_label_gige.pack(side=tk.LEFT, padx=10)

        self.fps_label_usb = ttk.Label(
            controls_frame, text="USB FPS: —",
            font=('Arial', 10, 'bold'), foreground="#000000")
        self.fps_label_usb.pack(side=tk.LEFT, padx=10)

        self.recording_label = ttk.Label(
            controls_frame, text="Запись: Нет",
            font=('Arial', 10, 'bold'), foreground="#3010c2")
        self.recording_label.pack(side=tk.LEFT, padx=20)

        self.disk_label = ttk.Label(
            controls_frame, text="Диск: —",
            font=('Arial', 10, 'bold'), foreground="#555555")
        self.disk_label.pack(side=tk.LEFT, padx=20)

        self.video_control_btn = ttk.Button(
            controls_frame, text="Остановить видео",
            command=self.toggle_video, width=18, state=tk.DISABLED)
        self.video_control_btn.pack(side=tk.RIGHT, padx=5)

        self.record_btn = ttk.Button(
            controls_frame, text="Записать видео",
            command=self.toggle_recording, width=18, state=tk.DISABLED)
        self.record_btn.pack(side=tk.RIGHT, padx=5)

        self.snapshot_btn = ttk.Button(
            controls_frame, text="Снимок",
            command=self.take_snapshot, width=15, state=tk.DISABLED)
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)

        self.video_frame = ttk.Frame(self.video_tab, relief=tk.SUNKEN,
                                     borderwidth=2)
        self.video_frame.pack(fill=tk.BOTH, expand=True,
                              padx=10, pady=10)

        # Сетка 2x1 всегда. Оба Canvas имеют фиксированную
        # половину экрана, независимо от того, есть там камера или нет.
        self.video_frame.columnconfigure(0, weight=1)
        self.video_frame.columnconfigure(1, weight=1)
        self.video_frame.rowconfigure(1, weight=1)

        # Левый столбец — GigE
        gige_slot = self.slots['gige']
        gige_slot.label = ttk.Label(
            self.video_frame, text="GigE: —",
            font=('Arial', 10, 'bold'))
        gige_slot.label.grid(row=0, column=0, sticky='ew',
                             padx=2, pady=(2, 0))
        gige_slot.canvas = tk.Canvas(
            self.video_frame, background='black',
            highlightthickness=0)
        gige_slot.canvas.grid(row=1, column=0, sticky='nsew',
                              padx=2, pady=2)

        # Правый столбец — USB
        usb_slot = self.slots['usb']
        usb_slot.label = ttk.Label(
            self.video_frame, text="USB: —",
            font=('Arial', 10, 'bold'))
        usb_slot.label.grid(row=0, column=1, sticky='ew',
                            padx=2, pady=(2, 0))
        usb_slot.canvas = tk.Canvas(
            self.video_frame, background='black',
            highlightthickness=0)
        usb_slot.canvas.grid(row=1, column=1, sticky='nsew',
                             padx=2, pady=2)

        self.video_status = ttk.Label(
            self.video_tab, text="Статус: Ожидание",
            relief=tk.SUNKEN, anchor=tk.W)
        self.video_status.pack(side=tk.BOTTOM, fill=tk.X,
                               padx=10, pady=5)

    def create_log_tab(self):
        log_controls = ttk.Frame(self.log_tab, padding="5")
        log_controls.pack(fill=tk.X)

        ttk.Button(log_controls, text="Очистить логи",
                   command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_controls, text="Сохранить логи",
                   command=self.save_logs).pack(side=tk.LEFT, padx=5)

        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.log_text = tk.Text(
            log_frame, wrap=tk.WORD, bg='#1e1e1e', fg='white',
            font=('Consolas', 10), height=20)
        self.log_text.tag_config('red', foreground='#ff6b6b')
        self.log_text.tag_config('orange', foreground='#ffa94d')
        self.log_text.tag_config('green', foreground='#69db7c')
        self.log_text.tag_config('blue', foreground='#74c0fc')

        log_scrollbar = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL,
            command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _log(self, message, tag):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{timestamp}] {message}"
        try:
            self.log_text.insert(tk.END, line + "\n", (tag,))
            self.log_text.see(tk.END)
        except Exception:
            pass
        self._log_to_file(line)

    def log_info(self, m):
        self._log(m, 'blue')

    def log_success(self, m):
        self._log(m, 'green')

    def log_warning(self, m):
        self._log(f"[WARN] {m}", 'orange')

    def log_error(self, m):
        self._log(f"[ERROR] {m}", 'red')

    def clear_logs(self):
        self.log_text.delete(1.0, tk.END)
        self.log_info("Логи очищены")

    def save_logs(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"),
                       ("Text files", "*.txt"),
                       ("All files", "*.*")])
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.get(1.0, tk.END))
                self.log_success(f"Логи сохранены в {file_path}")
            except Exception as e:
                self.log_error(f"Не удалось сохранить логи: {e}")

    def get_type_display(self, camera_type: str) -> str:
        return {
            'aravis': 'GigE', 'gige': 'GigE',
            'harvester': 'GigE', 'lucid': 'GigE (Lucid)',
            'hikrobot': 'GigE (Hikrobot)',
            'usbcam': 'USB', 'unknown': 'Unknown',
        }.get(camera_type, camera_type.upper())

    def _slot_for_camera(self, camera_type: str) -> str:
        if camera_type == 'usbcam':
            return 'usb'
        return 'gige'

    def scan_cameras(self):
        for slot in self.slots.values():
            slot.is_streaming = False
            slot.display_enabled = False
        if self.is_recording:
            self.stop_recording()
        for slot in self.slots.values():
            if slot.camera is not None:
                try:
                    slot.camera.release()
                except Exception:
                    pass
                slot.camera = None
                slot.camera_info = None

        self.log_info("Запуск сканирования камер...")
        self.scan_btn.config(state=tk.DISABLED)
        self.loading_label.config(text="Поиск камер...")
        self.status_bar.config(text="Выполняется сканирование камер...")

        Thread(target=self._scan_cameras_thread, daemon=True).start()

    def _scan_cameras_thread(self):
        try:
            self.cameras = CameraScanner.scan_all()
            self.root.after(0, self._update_camera_list)
            self.log_success(
                f"Сканирование завершено. Найдено камер: "
                f"{len(self.cameras)}")
        except Exception as e:
            error_msg = f"Ошибка сканирования: {e}"
            self.log_error(error_msg)
            self.root.after(0, lambda: self._show_error(error_msg))

    def _update_camera_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.cameras:
            self.tree.insert('', 'end',
                             values=('Нет камер', '—', '—', '—', '—'))
            self.status_bar.config(text="Камеры не найдены")
            self.log_warning("Камеры не найдены")
        else:
            for cam in self.cameras:
                status = cam.get('status', 'Unknown')
                status_display = {
                    'Available': 'Доступна',
                    'Connected': 'Подключена',
                    'Failed': 'Ошибка'
                }.get(status, 'Неизвестно')
                ip = cam.get('ip', 'N/A') or 'N/A'
                camera_type = cam.get('type', 'unknown')
                self.tree.insert(
                    '', 'end',
                    values=(cam.get('name', 'Unknown'),
                            self.get_type_display(camera_type),
                            cam.get('serial', 'N/A'),
                            ip,
                            status_display),
                    tags=(camera_type,))

            usb_count = sum(1 for c in self.cameras
                            if c.get('type') == 'usbcam')
            gige_count = sum(1 for c in self.cameras
                             if c.get('type') != 'usbcam')
            parts = []
            if gige_count:
                parts.append(f"GigE: {gige_count}")
            if usb_count:
                parts.append(f"USB: {usb_count}")
            self.status_bar.config(
                text=f"Найдено камер: {len(self.cameras)} "
                     f"({', '.join(parts)})")

        self.scan_btn.config(state=tk.NORMAL)
        self.loading_label.config(text="")
        self.connect_btn.config(state=tk.DISABLED)

    def on_select_camera(self, event):
        selection = self.tree.selection()
        if selection:
            self.connect_btn.config(state=tk.NORMAL)
            names = [self.tree.item(i, 'values')[0] for i in selection]
            self.status_bar.config(
                text=f"Выбрано: {', '.join(names)}")
        else:
            self.connect_btn.config(state=tk.DISABLED)

    def connect_camera(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("Предупреждение",
                                   "Сначала выберите камеру")
            return

        selected_cams = []
        for item in selection:
            idx = self.tree.index(item)
            if idx < len(self.cameras):
                selected_cams.append(self.cameras[idx])

        if not selected_cams:
            return

        for slot in self.slots.values():
            slot.is_streaming = False
            slot.display_enabled = False
            if slot.camera is not None:
                try:
                    slot.camera.release()
                except Exception:
                    pass
                slot.camera = None
                slot.camera_info = None

        assigned = {'gige': None, 'usb': None}
        for cam in selected_cams:
            camera_type = cam.get('type', 'aravis')
            slot_name = self._slot_for_camera(camera_type)
            if assigned[slot_name] is None:
                assigned[slot_name] = cam

        for slot_name, cam in assigned.items():
            if cam is None:
                self.slots[slot_name].label.config(
                    text=f"{slot_name.upper()}: —")
                continue
            try:
                self.log_info(
                    f"Подключение к {cam['name']} "
                    f"({self.get_type_display(cam.get('type'))})...")
                camera_type = cam.get('type', 'aravis')
                if camera_type == 'usbcam':
                    new_cam = create_camera(
                        'usbcam',
                        device_id=cam.get('device_id', 0))
                else:
                    new_cam = create_camera(
                        'aravis',
                        device_id=cam.get('device_id', 0),
                        saved_ip=cam.get('_saved_ip'),
                        pixel_format="Mono8")

                slot = self.slots[slot_name]
                slot.camera = new_cam
                slot.camera_info = cam
                slot.label.config(
                    text=f"{self.get_type_display(camera_type)}: "
                         f"{cam['name']}")
                self.log_success(
                    f"Подключено: {cam['name']} → слот {slot_name}")
            except Exception as e:
                self.log_error(
                    f"Не удалось подключить {cam['name']}: {e}")

        any_active = any(self.slots[s].camera is not None
                         for s in self.slots)

        if any_active:
            self.video_control_btn.config(state=tk.NORMAL)
            self.snapshot_btn.config(state=tk.NORMAL)
            self.record_btn.config(state=tk.NORMAL)

            self.notebook.select(self.video_tab)

            if not any(s.is_streaming for s in self.slots.values()):
                self.start_video_stream()

            self.video_info_label.config(text="Камеры подключены")
            self.log_success("Готово к работе: видео запущено")
        else:
            self.video_control_btn.config(state=tk.DISABLED)
            self.snapshot_btn.config(state=tk.DISABLED)
            self.record_btn.config(state=tk.DISABLED)
            self.video_info_label.config(text="Камеры не подключены")
            self.log_warning("Ни одна камера не подключена")

    def refresh_status(self):
        self.log_info("Обновление статуса камер...")
        self.scan_cameras()

    def toggle_video(self):
        if any(s.is_streaming for s in self.slots.values()):
            self.stop_video_stream()
        else:
            self.start_video_stream()

    def start_video_stream(self):
        any_active = False
        for slot_name, slot in self.slots.items():
            if slot.camera is None:
                continue
            any_active = True
            slot.is_streaming = True
            slot.display_enabled = True
            slot.frame_ready = False
            slot.frame_bgr = None
            slot.frame_size = None
            slot.frame_count = 0
            slot.fps_start_time = time.time()
            slot.current_fps = 0.0
            slot.thread = Thread(
                target=self._capture_loop,
                args=(slot_name,),
                daemon=True)
            slot.thread.start()

        if any_active:
            self.video_control_btn.config(text="Остановить видео")
            self.video_status.config(text="Статус: Видео идет")
            self.log_info("Запуск видео потока (с превью)...")

    def stop_video_stream(self):
        for slot in self.slots.values():
            slot.is_streaming = False
            slot.display_enabled = False
            if slot.canvas and slot.canvas_image_id:
                try:
                    slot.canvas.delete(slot.canvas_image_id)
                except Exception:
                    pass
                slot.canvas_image_id = None
            if slot.canvas:
                try:
                    slot.canvas.image = None
                except Exception:
                    pass

        self.video_control_btn.config(text="Запустить видео")
        self.video_status.config(text="Статус: Видео остановлено")
        self.log_info("Видео поток остановлен")

    def _capture_loop(self, slot_name: str):
        slot = self.slots[slot_name]
        while slot.is_streaming and slot.camera:
            try:
                frame = slot.camera.get_frame()
                if frame is None:
                    time.sleep(0.001)
                    continue

                if slot.frame_size is None:
                    slot.frame_size = (frame.shape[1], frame.shape[0])

                slot.frame_count += 1
                now = time.time()
                if now - slot.fps_start_time >= 0.5:
                    slot.current_fps = (
                        slot.frame_count / (now - slot.fps_start_time))
                    slot.frame_count = 0
                    slot.fps_start_time = now

                if self.is_recording and slot.recorder:
                    slot.recorder.write_frame(frame)

                if slot.display_enabled:
                    if frame.ndim == 2:
                        frame_bgr = cv2.cvtColor(
                            frame, cv2.COLOR_GRAY2BGR)
                    else:
                        frame_bgr = frame.copy()

                    with slot.frame_lock:
                        slot.frame_bgr = frame_bgr
                        slot.frame_ready = True

            except Exception as e:
                if slot.is_streaming:
                    self.log_error(f"[{slot_name}] Ошибка захвата: {e}")
                break

    def update_ui_loop(self):
        try:
            for slot_name, slot in self.slots.items():
                if not (slot.display_enabled
                        and slot.frame_ready
                        and slot.canvas):
                    continue

                with slot.frame_lock:
                    frame_bgr = slot.frame_bgr
                    slot.frame_ready = False

                if frame_bgr is None:
                    continue

                cw = slot.canvas.winfo_width()
                ch = slot.canvas.winfo_height()

                if cw <= 1 or ch <= 1:
                    with slot.frame_lock:
                        slot.frame_bgr = frame_bgr
                        slot.frame_ready = True
                    continue

                h, w = frame_bgr.shape[:2]
                scale = min(cw / w, ch / h)
                new_w = max(1, int(w * scale))
                new_h = max(1, int(h * scale))
                if new_w != w or new_h != h:
                    frame_resized = cv2.resize(
                        frame_bgr, (new_w, new_h),
                        interpolation=cv2.INTER_AREA)
                else:
                    frame_resized = frame_bgr

                frame_rgb = cv2.cvtColor(
                    frame_resized, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame_rgb)
                imgtk = ImageTk.PhotoImage(image=img)

                if slot.canvas_image_id:
                    try:
                        slot.canvas.delete(slot.canvas_image_id)
                    except Exception:
                        pass
                    slot.canvas_image_id = None

                iw, ih = imgtk.width(), imgtk.height()
                x = max(0, (cw - iw) // 2)
                y = max(0, (ch - ih) // 2)
                slot.canvas_image_id = slot.canvas.create_image(
                    x, y, anchor=tk.NW, image=imgtk)
                slot.canvas.image = imgtk

            gige = self.slots['gige']
            usb = self.slots['usb']
            if gige.camera:
                self.fps_label_gige.config(
                    text=f"GigE FPS: {gige.current_fps:.1f}")
            else:
                self.fps_label_gige.config(text="GigE FPS: —")
            if usb.camera:
                self.fps_label_usb.config(
                    text=f"USB FPS: {usb.current_fps:.1f}")
            else:
                self.fps_label_usb.config(text="USB FPS: —")

            if self.is_recording:
                parts = []
                for name, slot in self.slots.items():
                    if slot.recorder:
                        idx = slot.recorder.get_segment_count()
                        rem = slot.recorder.get_segment_remaining()
                        parts.append(
                            f"{name}: сегм.{idx}, "
                            f"{int(rem // 60)} мин")
                self.recording_label.config(
                    text="Запись: " + " | ".join(parts),
                    foreground='#ff0000')

            if (any(s.is_streaming for s in self.slots.values())
                    and time.time() % 2 < 0.05):
                free_mb = self._free_mb(self._current_record_dir())
                self._update_disk_label(free_mb)

        except Exception as e:
            print(f"UI loop error: {e}")

        if not self._closing:
            self.root.after(30, self.update_ui_loop)

    def toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _current_record_dir(self):
        for slot in self.slots.values():
            if slot.recorder and slot.recorder.output_dir:
                return slot.recorder.output_dir
        return os.path.expanduser("~")

    def _free_mb(self, path):
        try:
            st = os.statvfs(path)
            return (st.f_bavail * st.f_frsize) / (1024 * 1024)
        except Exception:
            return float('inf')

    def _update_disk_label(self, free_mb):
        if free_mb == float('inf'):
            self.disk_label.config(text="Диск: —",
                                   foreground="#555555")
            return
        if free_mb < MIN_FREE_MB:
            self.disk_label.config(
                text=f"Диск: {free_mb:.0f} MB (мало!)",
                foreground="#ff0000")
        elif free_mb < MIN_FREE_MB * 3:
            self.disk_label.config(
                text=f"Диск: {free_mb:.0f} MB",
                foreground="#ff8800")
        else:
            self.disk_label.config(
                text=f"Диск: {free_mb:.0f} MB",
                foreground="#007700")

    def _on_disk_full(self, free_mb):
        self.root.after(0, lambda: self._handle_disk_full(free_mb))

    def _handle_disk_full(self, free_mb):
        if not self.is_recording:
            return
        self.log_error(
            f"Мало места на диске ({free_mb:.0f} MB). "
            f"Запись остановлена автоматически.")
        self.stop_recording()
        messagebox.showwarning(
            "Диск заполнен",
            f"Свободное место меньше {MIN_FREE_MB} MB.\n"
            f"Запись автоматически остановлена.")

    def _on_segment_change(self, slot_name, index, path):
        self.root.after(
            0, lambda: self._handle_segment_change(
                slot_name, index, path))

    def _handle_segment_change(self, slot_name, index, path):
        self.log_info(f"[{slot_name}] Новый сегмент {index}: {path}")

    def start_recording(self):
        active = [s for s in self.slots.values() if s.camera]
        if not active:
            messagebox.showwarning("Предупреждение",
                                   "Сначала подключитесь к камере")
            return
        if self.is_recording:
            return

        try:
            recordings_dir = filedialog.askdirectory(
                title="Укажите директорию для сохранения видео")
            if not recordings_dir:
                return

            free_mb = self._free_mb(recordings_dir)
            if free_mb < MIN_FREE_MB:
                messagebox.showerror(
                    "Мало места",
                    f"Свободно всего {free_mb:.0f} MB.\n"
                    f"Нужно минимум {MIN_FREE_MB} MB.")
                return

            for slot in self.slots.values():
                slot.display_enabled = False
                if slot.canvas and slot.canvas_image_id:
                    try:
                        slot.canvas.delete(slot.canvas_image_id)
                    except Exception:
                        pass
                    slot.canvas_image_id = None

            self.log_info("Превью отключено на время записи")

            for slot_name, slot in self.slots.items():
                if slot.camera is None:
                    continue
                if not slot.is_streaming:
                    slot.is_streaming = True
                    slot.frame_ready = False
                    slot.frame_size = None
                    slot.frame_count = 0
                    slot.fps_start_time = time.time()
                    slot.current_fps = 0.0
                    slot.thread = Thread(
                        target=self._capture_loop,
                        args=(slot_name,),
                        daemon=True)
                    slot.thread.start()

            self.video_control_btn.config(text="Остановить видео")
            self.video_status.config(
                text="Статус: Инициализация записи...")
            self.root.update_idletasks()

            started = 0
            for slot_name, slot in self.slots.items():
                if slot.camera is None:
                    continue

                start_wait = time.time()
                while (slot.frame_size is None
                       and time.time() - start_wait < 3.0):
                    time.sleep(0.02)

                if slot.frame_size is None:
                    self.log_error(
                        f"[{slot_name}] Не удалось определить размер")
                    continue

                first_frame = slot.camera.get_frame()
                if first_frame is None:
                    self.log_error(
                        f"[{slot_name}] Камера не отдаёт кадры")
                    continue

                is_color = (first_frame.ndim == 3)
                width, height = slot.frame_size
                target_fps = int(slot.current_fps) if slot.current_fps >= 5 else 30
                cam_name = (slot.camera_info or {}).get(
                    'name', slot_name)

                slot.record_dir = recordings_dir
                slot.segment_paths = []
                slot.recorder = SegmentingRecorder(
                    output_dir=recordings_dir,
                    fps=target_fps,
                    is_color=is_color,
                    min_free_mb=MIN_FREE_MB,
                    on_disk_full=self._on_disk_full,
                    segment_seconds=SEGMENT_DURATION_SEC,
                    camera_name=f"{cam_name}_{slot_name}",
                    on_segment_change=lambda i, p, n=slot_name:
                        self._on_segment_change(n, i, p)
                )

                if not slot.recorder.start(width, height):
                    self.log_error(
                        f"[{slot_name}] Не удалось начать сегмент")
                    slot.recorder = None
                    continue

                slot.is_recording = True
                slot.recording_start_time = time.time()
                started += 1
                self.log_success(
                    f"[{slot_name}] Запись начата: "
                    f"{slot.recorder.get_segment_paths()[-1]}")

            if started == 0:
                self.is_recording = False
                self.record_btn.config(text="Записать видео")
                self.recording_label.config(
                    text="Запись: Нет",
                    foreground="#3010c2")
                messagebox.showerror("Ошибка",
                                     "Не удалось начать запись")
                return

            self.is_recording = True
            self.record_btn.config(text="Остановить запись")
            self.recording_label.config(
                text=f"Запись: ИДЕТ ({started} кам.)",
                foreground='#ff0000')
            self.video_status.config(
                text=f"Статус: Запись идет ({started} камер)")

        except Exception as e:
            self.log_error(f"Ошибка начала записи: {e}")
            messagebox.showerror(
                "Ошибка", f"Не удалось начать запись: {e}")

    def stop_recording(self):
        if not self.is_recording:
            return
        try:
            total_paths = []
            for slot_name, slot in self.slots.items():
                if slot.recorder is None:
                    continue
                try:
                    paths = slot.recorder.stop()
                    total_paths.extend(paths)
                    self.log_success(
                        f"[{slot_name}] Запись остановлена, "
                        f"сегментов: {len(paths)}")
                    for p in paths:
                        self.log_info(f"  [{slot_name}] {p}")
                except Exception as e:
                    self.log_error(
                        f"[{slot_name}] Ошибка остановки: {e}")
                finally:
                    slot.recorder = None
                    slot.is_recording = False
                    slot.recording_start_time = None

            self.is_recording = False
            self.record_btn.config(text="Записать видео")
            self.recording_label.config(
                text="Запись: Нет", foreground="#3010c2")

            for slot in self.slots.values():
                if slot.is_streaming and slot.camera:
                    slot.display_enabled = True

            if total_paths:
                self.video_status.config(
                    text=f"Статус: Записано файлов: "
                         f"{len(total_paths)}")
                first_dir = os.path.dirname(total_paths[0])
                if messagebox.askyesno(
                    "Запись завершена",
                    f"Всего файлов: {len(total_paths)}\n\n"
                    f"Папка: {first_dir}\n\n"
                    f"Открыть папку?"
                ):
                    if platform.system() == 'Windows':
                        os.startfile(first_dir)
                    elif platform.system() == 'Darwin':
                        subprocess.Popen(['open', first_dir])
                    else:
                        subprocess.Popen(['xdg-open', first_dir])
            else:
                self.log_warning("Запись не была сохранена")
                self.video_status.config(
                    text="Статус: Запись не сохранена")

        except Exception as e:
            self.log_error(f"Ошибка остановки записи: {e}")
            messagebox.showerror(
                "Ошибка", f"Не удалось остановить запись: {e}")
            self.is_recording = False
            self.record_btn.config(text="Записать видео")
            self.recording_label.config(
                text="Запись: Нет", foreground="#3010c2")

    def take_snapshot(self):
        any_active = any(s.camera for s in self.slots.values())
        if not any_active:
            messagebox.showwarning("Предупреждение",
                                   "Камера не подключена")
            return
        try:
            snapshots_dir = filedialog.askdirectory(
                title="Выберите каталог для сохранения снимков")
            if not snapshots_dir:
                return

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            saved = 0
            for slot_name, slot in self.slots.items():
                if slot.camera is None:
                    continue
                frame = slot.camera.get_frame()
                if frame is None:
                    continue
                cam_name = (slot.camera_info or {}).get(
                    'name', slot_name)
                safe = re.sub(r'[^\w\-_\. ]', '_', cam_name)
                filepath = os.path.join(
                    snapshots_dir,
                    f"{safe}_{slot_name}_{timestamp}.png")
                cv2.imwrite(filepath, frame)
                self.log_success(f"Снимок сохранён: {filepath}")
                saved += 1

            if saved == 0:
                self.log_warning("Не удалось получить кадр для снимка")
        except Exception as e:
            self.log_error(f"Ошибка при сохранении снимка: {e}")

    def _show_error(self, error_message):
        self.scan_btn.config(state=tk.NORMAL)
        self.loading_label.config(text="")
        self.status_bar.config(text=f"Ошибка: {error_message}")
        messagebox.showerror("Ошибка", error_message)


def main():
    root = tk.Tk()
    app = CameraDiscoveryApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.log_warning("Прервано пользователем (Ctrl+C)")
        app.on_closing()
    except Exception as e:
        app.log_error(f"Критическая ошибка в mainloop: {e}")
        app._log_to_file(traceback.format_exc())
        app.on_closing()
    finally:
        app._close_log_file()


if __name__ == "__main__":
    main()