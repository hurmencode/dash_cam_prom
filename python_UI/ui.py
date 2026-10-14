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
    """Состояние одного слота (их всего два)."""
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

        self.preview_window = None
        self.preview_canvas = None
        self.preview_image_id = None

        self.slot_info_label = None
        self.fps_label = None
        self.show_btn = None


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

        self.root.geometry("1100x700")

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
            'slot_1': SlotState('slot_1'),
            'slot_2': SlotState('slot_2'),
        }
        self.cameras = []
        self.is_recording = False
        self.connected_cameras = {}

        self.create_widgets()
        self.install_crash_handlers()

        self.root.after(500, self.scan_cameras)
        self.root.after(30, self.update_ui_loop)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.log_info(f"Сессия логов: {self.log_file_path}")

    # ============ АВАРИЙНЫЕ ОБРАБОТЧИКИ ============
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

    # ============ ЗАКРЫТИЕ ============
    def on_closing(self):
        if self._closing:
            return
        self._closing = True

        for slot in self.slots.values():
            slot.is_streaming = False
            slot.display_enabled = False
            self._close_preview_window(slot, silent=True)

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

    # ============ ИНТЕРФЕЙС ============
    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.camera_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.camera_tab, text="Камеры")
        self.create_camera_tab()

        self.video_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.video_tab, text="Управление")
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

        self.disconnect_btn = ttk.Button(
            top_frame, text="Отключить все",
            command=self.disconnect_all, width=20)
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)

        self.refresh_btn = ttk.Button(
            top_frame, text="Обновить статус",
            command=self.refresh_status, width=20)
        self.refresh_btn.pack(side=tk.LEFT, padx=5)

        self.loading_label = ttk.Label(top_frame, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)

        hint = ttk.Label(
            self.camera_tab,
            text="Выберите одну или две камеры (Ctrl+Click). "
                 "Максимум 2 камеры одновременно.\n"
                 "Для просмотра видео перейдите на вкладку "
                 "«Управление».",
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

        self.recording_label = ttk.Label(
            controls_frame, text="Запись: Нет",
            font=('Arial', 11, 'bold'), foreground="#3010c2")
        self.recording_label.pack(side=tk.LEFT, padx=5)

        self.disk_label = ttk.Label(
            controls_frame, text="Диск: —",
            font=('Arial', 11, 'bold'), foreground="#555555")
        self.disk_label.pack(side=tk.LEFT, padx=20)

        self.record_btn = ttk.Button(
            controls_frame, text="Записать видео",
            command=self.toggle_recording, width=18, state=tk.DISABLED)
        self.record_btn.pack(side=tk.RIGHT, padx=5)

        self.snapshot_btn = ttk.Button(
            controls_frame, text="Снимок",
            command=self.take_snapshot, width=15, state=tk.DISABLED)
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)

        # Вторая строка — кнопки «Показать все / Скрыть все»
        preview_frame = ttk.Frame(self.video_tab, padding="10")
        preview_frame.pack(fill=tk.X)

        self.preview_all_btn = ttk.Button(
            preview_frame, text="Показать все",
            command=self.toggle_all_previews,
            width=20, state=tk.DISABLED)
        self.preview_all_btn.pack(side=tk.LEFT, padx=5)

        self.preview_hint = ttk.Label(
            preview_frame,
            text="Открывает / закрывает окна превью для всех "
                 "подключённых камер",
            foreground="#666666")
        self.preview_hint.pack(side=tk.LEFT, padx=10)

        slots_frame = ttk.LabelFrame(
            self.video_tab, text="Подключённые камеры", padding="10")
        slots_frame.pack(fill=tk.X, padx=10, pady=10)

        for i, (slot_name, slot) in enumerate(self.slots.items()):
            row = ttk.Frame(slots_frame)
            row.pack(fill=tk.X, pady=3)

            name_lbl = ttk.Label(
                row, text=f"Слот {i + 1}: —",
                font=('Arial', 10, 'bold'), width=45, anchor='w')
            name_lbl.pack(side=tk.LEFT, padx=5)
            slot.slot_info_label = name_lbl

            fps_lbl = ttk.Label(
                row, text="FPS: —",
                font=('Arial', 10), width=15, anchor='w')
            fps_lbl.pack(side=tk.LEFT, padx=5)
            slot.fps_label = fps_lbl

            show_btn = ttk.Button(
                row, text="Показать",
                command=lambda sn=slot_name: self.toggle_preview(sn),
                width=15, state=tk.DISABLED)
            show_btn.pack(side=tk.RIGHT, padx=5)
            slot.show_btn = show_btn

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

    # ============ ЛОГИ ============
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

    # ============ КАМЕРЫ ============
    def get_type_display(self, camera_type: str) -> str:
        return {
            'aravis': 'GigE', 'gige': 'GigE',
            'harvester': 'GigE', 'lucid': 'GigE (Lucid)',
            'hikrobot': 'GigE (Hikrobot)',
            'usbcam': 'USB', 'unknown': 'Unknown',
        }.get(camera_type, camera_type.upper())

    def _camera_key(self, cam_info):
        cam_type = cam_info.get('type', 'unknown')
        if cam_type == 'usbcam':
            return ('usb', cam_info.get('device_id', 0))
        serial = cam_info.get('serial')
        if serial and serial not in ('', 'N/A', 'Unknown'):
            return ('gige', serial)
        return ('gige', cam_info.get('ip', 'N/A'))

    def scan_cameras(self):
        self.disconnect_all(silent=True)

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
                key = self._camera_key(cam)
                if key in self.connected_cameras:
                    status_display = 'Подключена'
                else:
                    status_display = {
                        'Available': 'Доступна',
                        'Connected': 'Подключена',
                        'Failed': 'Ошибка'
                    }.get(cam.get('status', 'Unknown'), 'Неизвестно')

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

    def _get_free_slots(self):
        return [name for name, slot in self.slots.items()
                if slot.camera is None]

    def _is_camera_connected(self, cam_info):
        key = self._camera_key(cam_info)
        return key in self.connected_cameras

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

        cams_to_connect = []
        for cam in selected_cams:
            if self._is_camera_connected(cam):
                self.log_warning(
                    f"{cam.get('name')} уже подключена — пропускаю")
                continue
            cams_to_connect.append(cam)

        if not cams_to_connect:
            messagebox.showinfo(
                "Уже подключено",
                "Все выбранные камеры уже подключены.")
            return

        free_slots = self._get_free_slots()
        if len(cams_to_connect) > len(free_slots):
            messagebox.showwarning(
                "Слишком много камер",
                f"Свободно слотов: {len(free_slots)}. "
                f"Можно подключить ещё: {len(free_slots)}.\n"
                f"Максимум 2 камеры одновременно.")
            cams_to_connect = cams_to_connect[:len(free_slots)]

        connected_names = []
        failed_names = []

        for cam, slot_name in zip(cams_to_connect, free_slots):
            slot = self.slots[slot_name]
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

                slot.camera = new_cam
                slot.camera_info = cam

                key = self._camera_key(cam)
                self.connected_cameras[key] = slot_name

                cam_name = cam.get('name', 'Unknown')
                cam_type = self.get_type_display(camera_type)
                self._update_slot_label(
                    slot, f"Слот {list(self.slots.keys()).index(slot_name) + 1}: "
                          f"{cam_type} — {cam_name}")

                slot.show_btn.config(state=tk.NORMAL)

                self.log_success(
                    f"Подключено: {cam_name} → {slot_name}")
                connected_names.append(cam_name)

            except Exception as e:
                self.log_error(
                    f"Не удалось подключить {cam['name']}: {e}")
                failed_names.append(cam['name'])

        self._refresh_table_statuses()

        any_active = any(s.camera is not None
                         for s in self.slots.values())
        if any_active:
            self.snapshot_btn.config(state=tk.NORMAL)
            self.record_btn.config(state=tk.NORMAL)
            self.preview_all_btn.config(state=tk.NORMAL)
            self._update_preview_all_btn()

        if connected_names:
            msg = "Успешно подключено:\n  • " + "\n  • ".join(connected_names)
            msg += ("\n\nДля просмотра видео перейдите на вкладку "
                    "«Управление» и нажмите «Показать все» "
                    "или «Показать» напротив нужной камеры.")
            if failed_names:
                msg += ("\n\nНе удалось подключить:\n  • "
                        + "\n  • ".join(failed_names))
            messagebox.showinfo("Подключение", msg)

    def _refresh_table_statuses(self):
        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            if not values:
                continue
            cam_name = values[0]
            for cam in self.cameras:
                if cam.get('name') == cam_name:
                    key = self._camera_key(cam)
                    new_values = list(values)
                    if key in self.connected_cameras:
                        new_values[4] = 'Подключена'
                    else:
                        if values[4] == 'Подключена':
                            new_values[4] = 'Доступна'
                    self.tree.item(item, values=tuple(new_values))
                    break

    def _update_slot_label(self, slot, text):
        if slot.slot_info_label:
            slot.slot_info_label.config(text=text)

    def disconnect_all(self, silent=False):
        for slot_name, slot in self.slots.items():
            if slot.recorder is not None:
                try:
                    slot.recorder.stop()
                except Exception:
                    pass
                slot.recorder = None
                slot.is_recording = False

            slot.is_streaming = False
            slot.display_enabled = False

            self._close_preview_window(slot, silent=silent)

            if slot.camera is not None:
                try:
                    slot.camera.release()
                except Exception as e:
                    if not silent:
                        self.log_error(
                            f"Ошибка освобождения {slot.name}: {e}")
                slot.camera = None
            slot.camera_info = None

            idx = list(self.slots.keys()).index(slot_name)
            self._update_slot_label(slot, f"Слот {idx + 1}: —")
            if slot.fps_label:
                slot.fps_label.config(text="FPS: —")
            if slot.show_btn:
                slot.show_btn.config(state=tk.DISABLED, text="Показать")

        self.connected_cameras.clear()
        self.is_recording = False
        self.record_btn.config(text="Записать видео")
        self.recording_label.config(
            text="Запись: Нет", foreground="#3010c2")
        self.snapshot_btn.config(state=tk.DISABLED)
        self.record_btn.config(state=tk.DISABLED)
        self.preview_all_btn.config(state=tk.DISABLED, text="Показать все")

        if not silent:
            self.log_info("Все камеры отключены")
            self._refresh_table_statuses()

    def refresh_status(self):
        self.log_info("Обновление статуса камер...")
        self.scan_cameras()

    # ============ ОКНА ПРЕВЬЮ ============
    def toggle_preview(self, slot_name):
        slot = self.slots[slot_name]
        if slot.camera is None:
            return

        if slot.preview_window is not None:
            self._close_preview_window(slot)
        else:
            self._open_preview_window(slot)

        self._update_preview_all_btn()

    def toggle_all_previews(self):
        """Открывает все окна, если хотя бы одно закрыто,
        иначе закрывает все."""
        # Проверяем, есть ли хотя бы одно открытое
        any_open = any(s.preview_window is not None
                       for s in self.slots.values()
                       if s.camera is not None)

        if any_open:
            # Закрываем все
            for slot in self.slots.values():
                if slot.camera is not None and \
                        slot.preview_window is not None:
                    self._close_preview_window(slot)
            self.log_info("Все окна превью закрыты")
        else:
            # Открываем все для слотов с камерой
            for slot in self.slots.values():
                if slot.camera is not None and \
                        slot.preview_window is None:
                    self._open_preview_window(slot)
            self.log_info("Все окна превью открыты")

        self._update_preview_all_btn()

    def _update_preview_all_btn(self):
        """Меняет текст кнопки на «Показать все» или «Скрыть все»
        в зависимости от того, есть ли открытые окна."""
        any_open = any(s.preview_window is not None
                       for s in self.slots.values()
                       if s.camera is not None)
        any_camera = any(s.camera is not None
                         for s in self.slots.values())

        if not any_camera:
            self.preview_all_btn.config(
                text="Показать все", state=tk.DISABLED)
            return

        if any_open:
            self.preview_all_btn.config(
                text="Скрыть все", state=tk.NORMAL)
        else:
            self.preview_all_btn.config(
                text="Показать все", state=tk.NORMAL)

    def _open_preview_window(self, slot):
        if slot.camera is None:
            return

        if slot.preview_window is not None:
            try:
                slot.preview_window.lift()
                slot.preview_window.focus_force()
            except Exception:
                pass
            return

        cam_info = slot.camera_info or {}
        cam_name = cam_info.get('name', slot.name)
        cam_type = self.get_type_display(cam_info.get('type', 'unknown'))

        win = tk.Toplevel(self.root)
        win.title(f"{cam_type}: {cam_name}")
        win.geometry("900x650")
        win.minsize(320, 240)

        canvas = tk.Canvas(win, background='black',
                           highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        slot.preview_window = win
        slot.preview_canvas = canvas
        slot.preview_image_id = None

        win.protocol("WM_DELETE_WINDOW",
                     lambda s=slot: self._close_preview_window(s))

        slot.display_enabled = True
        if not slot.is_streaming:
            slot.is_streaming = True
            slot.frame_ready = False
            slot.frame_size = None
            slot.frame_count = 0
            slot.fps_start_time = time.time()
            slot.current_fps = 0.0
            slot.thread = Thread(
                target=self._capture_loop,
                args=(slot.name,),
                daemon=True)
            slot.thread.start()

        if slot.show_btn:
            slot.show_btn.config(text="Скрыть")

        self.log_info(f"[{slot.name}] Окно превью открыто")

    def _close_preview_window(self, slot, silent=False):
        if slot.preview_image_id and slot.preview_canvas:
            try:
                slot.preview_canvas.delete(slot.preview_image_id)
            except Exception:
                pass
        slot.preview_image_id = None
        slot.preview_canvas = None

        if slot.preview_window is not None:
            try:
                slot.preview_window.destroy()
            except Exception:
                pass
            slot.preview_window = None

        slot.display_enabled = False

        if slot.show_btn:
            slot.show_btn.config(text="Показать")

        if not silent:
            self.log_info(
                f"[{slot.name}] Окно превью закрыто "
                f"(запись продолжается)")

        # Обновляем текст кнопки «Показать все / Скрыть все»
        self._update_preview_all_btn()

    # ============ ВИДЕО ============
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
                    self.log_error(
                        f"[{slot_name}] Ошибка захвата: {e}")
                break

    def update_ui_loop(self):
        try:
            for slot_name, slot in self.slots.items():
                if slot.fps_label:
                    if slot.camera is not None:
                        slot.fps_label.config(
                            text=f"FPS: {slot.current_fps:.1f}")
                    else:
                        slot.fps_label.config(text="FPS: —")

                if not (slot.display_enabled
                        and slot.frame_ready
                        and slot.preview_canvas is not None):
                    continue

                with slot.frame_lock:
                    frame_bgr = slot.frame_bgr
                    slot.frame_ready = False

                if frame_bgr is None:
                    continue

                cw = slot.preview_canvas.winfo_width()
                ch = slot.preview_canvas.winfo_height()

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

                if slot.preview_image_id:
                    try:
                        slot.preview_canvas.delete(
                            slot.preview_image_id)
                    except Exception:
                        pass
                    slot.preview_image_id = None

                iw, ih = imgtk.width(), imgtk.height()
                x = max(0, (cw - iw) // 2)
                y = max(0, (ch - ih) // 2)
                slot.preview_image_id = \
                    slot.preview_canvas.create_image(
                        x, y, anchor=tk.NW, image=imgtk)
                slot.preview_canvas.image = imgtk

            if self.is_recording:
                parts = []
                for name, slot in self.slots.items():
                    if slot.recorder:
                        idx = slot.recorder.get_segment_count()
                        rem = slot.recorder.get_segment_remaining()
                        parts.append(
                            f"{name}: сегм.{idx}, "
                            f"{int(rem // 60)} мин")
                if parts:
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

    # ============ ЗАПИСЬ ============
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

            self.log_info("Инициализация записи...")

            started = 0
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
                target_fps = (int(slot.current_fps)
                              if slot.current_fps >= 5 else 30)
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

    # ============ СНИМОК ============
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