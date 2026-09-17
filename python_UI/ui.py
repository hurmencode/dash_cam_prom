import re

import cv2
from datetime import datetime
import os
from PIL import Image, ImageTk
import platform
import subprocess
import sys
import time
import tkinter as tk
from tkinter import PhotoImage, ttk, messagebox, filedialog
from threading import Thread, Lock

# Добавляем путь к бэкенду
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from backend.camera_manager import CameraScanner, create_camera, VideoRecorder


class CameraDiscoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Видеорегистратор 3000")

        try:
            icon = PhotoImage(file="favicon.png")
            self.root.iconphoto(False, icon)
        except Exception as e:
            print(f"Не удалось загрузить иконку: {e}")

        self.root.geometry("1300x700")

        # Переменные
        self.cameras = []
        self.selected_camera = None
        self.current_camera = None
        self.is_streaming = False
        self.video_thread = None

        # Отображение
        self.display_enabled = False
        self.user_wants_display = False

        # FPS
        self.frame_count = 0
        self.fps_start_time = time.time()
        self.current_fps = 0.0
        self.fps_update_interval = 0.5

        # Буфер кадра
        self.last_frame_bgr = None
        self.frame_lock = Lock()
        self.frame_ready = False

        # Размер кадра
        self._frame_size = None

        # Запись
        self.video_recorder = None
        self.is_recording = False
        self.current_record_path = None
        self.recording_start_time = None
        self.recording_duration = 0

        # Канвас
        self.canvas_image_id = None

        # Интерфейс
        self.create_widgets()

        # Автопоиск
        self.root.after(500, self.scan_cameras)

        # UI-цикл
        self.root.after(30, self.update_ui_loop)

        # Закрытие
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ============ ЗАКРЫТИЕ ============
    def on_closing(self):
        self.is_streaming = False
        self.display_enabled = False
        try:
            if self.is_recording:
                self.stop_recording()
        except Exception:
            pass
        try:
            if self.current_camera:
                self.current_camera.release()
        except Exception:
            pass
        self.root.destroy()

    # ============ ИНТЕРФЕЙС ============
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
            top_frame, text="Поиск камер", command=self.scan_cameras, width=20
        )
        self.scan_btn.pack(side=tk.LEFT, padx=5)

        self.connect_btn = ttk.Button(
            top_frame, text="Подключиться", command=self.connect_camera,
            width=20, state=tk.DISABLED
        )
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.show_video_btn = ttk.Button(
            top_frame, text="Показать видео", command=self.show_video,
            width=20, state=tk.DISABLED
        )
        self.show_video_btn.pack(side=tk.LEFT, padx=5)

        self.refresh_btn = ttk.Button(
            top_frame, text="Обновить статус", command=self.refresh_status, width=20
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=5)

        self.loading_label = ttk.Label(top_frame, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)

        columns = ('name', 'type', 'serial', 'ip', 'status')
        self.tree = ttk.Treeview(
            self.camera_tab, columns=columns, show='headings', height=15
        )

        self.status_bar = ttk.Label(
            self.camera_tab,
            text="Готов к работе. Нажмите 'Поиск камер'",
            relief=tk.SUNKEN,
        )
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        self.tree.heading('name', text='Имя камеры')
        self.tree.heading('type', text='Тип')
        self.tree.heading('serial', text='Серийный номер')
        self.tree.heading('ip', text='IP-адрес')
        self.tree.heading('status', text='Статус')

        for col in columns:
            self.tree.column(col, anchor='center')

        scrollbar = ttk.Scrollbar(self.camera_tab, orient=tk.VERTICAL,
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=10)

        self.tree.bind('<<TreeviewSelect>>', self.on_select_camera)
        self.create_context_menu()

    def create_video_tab(self):
        controls_frame = ttk.Frame(self.video_tab, padding="10")
        controls_frame.pack(fill=tk.X)

        self.video_info_label = ttk.Label(
            controls_frame, text="Камера не выбрана", font=('Arial', 10)
        )
        self.video_info_label.pack(side=tk.LEFT, padx=5)

        self.fps_label = ttk.Label(
            controls_frame, text="FPS: 0",
            font=('Arial', 10, 'bold'), foreground="#000000"
        )
        self.fps_label.pack(side=tk.LEFT, padx=20)

        self.recording_label = ttk.Label(
            controls_frame, text="Запись: Нет",
            font=('Arial', 10, 'bold'), foreground="#3010c2"
        )
        self.recording_label.pack(side=tk.LEFT, padx=20)

        self.video_control_btn = ttk.Button(
            controls_frame, text="Запустить видео", command=self.toggle_video,
            width=18, state=tk.DISABLED
        )
        self.video_control_btn.pack(side=tk.RIGHT, padx=5)

        self.record_btn = ttk.Button(
            controls_frame, text="Записать", command=self.toggle_recording,
            width=15, state=tk.DISABLED
        )
        self.record_btn.pack(side=tk.RIGHT, padx=5)

        self.snapshot_btn = ttk.Button(
            controls_frame, text="Снимок", command=self.take_snapshot,
            width=12, state=tk.DISABLED
        )
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)

        self.video_frame = ttk.Frame(self.video_tab, relief=tk.SUNKEN, borderwidth=2)
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.video_canvas = tk.Canvas(self.video_frame, background='black',
                                      highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)

        self.video_status = ttk.Label(
            self.video_tab, text="Статус: Ожидание",
            relief=tk.SUNKEN, anchor=tk.W
        )
        self.video_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

    def create_log_tab(self):
        log_controls = ttk.Frame(self.log_tab, padding="5")
        log_controls.pack(fill=tk.X)

        clear_log_btn = ttk.Button(log_controls, text="Очистить логи",
                                   command=self.clear_logs)
        clear_log_btn.pack(side=tk.LEFT, padx=5)

        save_log_btn = ttk.Button(log_controls, text="Сохранить логи",
                                  command=self.save_logs)
        save_log_btn.pack(side=tk.LEFT, padx=5)

        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.log_text = tk.Text(
            log_frame, wrap=tk.WORD, bg='#1e1e1e', fg='white',
            font=('Consolas', 10), height=20
        )

        self.log_text.tag_config('red', foreground='#ff6b6b')
        self.log_text.tag_config('orange', foreground='#ffa94d')
        self.log_text.tag_config('green', foreground='#69db7c')
        self.log_text.tag_config('blue', foreground='#74c0fc')
        self.log_text.tag_config('white', foreground='#ffffff')

        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                                      command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_info("Логи запущены. Приложение готово к работе.")
        self.log_info("Нажмите 'Поиск камер' для сканирования.")

    # ============ ЛОГИ ============
    def log_info(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", ('blue',))
        self.log_text.see(tk.END)

    def log_success(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", ('green',))
        self.log_text.see(tk.END)

    def log_warning(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", ('orange',))
        self.log_text.see(tk.END)

    def log_error(self, message):
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", ('red',))
        self.log_text.see(tk.END)

    def clear_logs(self):
        self.log_text.delete(1.0, tk.END)
        self.log_info("Логи очищены")

    def save_logs(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                       ("All files", "*.*")],
            title="Сохранить логи"
        )
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.get(1.0, tk.END))
                self.log_success(f"Логи сохранены в {file_path}")
            except Exception as e:
                self.log_error(f"Не удалось сохранить логи: {e}")

    # ============ КАМЕРЫ ============
    def create_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Копировать IP", command=self.copy_ip)
        self.context_menu.add_command(label="Копировать серийный номер",
                                      command=self.copy_serial)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Обновить", command=self.scan_cameras)
        self.tree.bind('<Button-3>', self.show_context_menu)

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def copy_ip(self):
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], 'values')
            if values and len(values) >= 4:
                ip = values[3]
                if ip and ip not in ('N/A', 'Unknown'):
                    self.root.clipboard_clear()
                    self.root.clipboard_append(ip)
                    self.status_bar.config(text=f"Скопирован IP: {ip}")
                    self.log_success(f"IP адрес скопирован: {ip}")

    def copy_serial(self):
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], 'values')
            if values and len(values) >= 3:
                serial = values[2]
                if serial and serial not in ('N/A', 'Unknown'):
                    self.root.clipboard_clear()
                    self.root.clipboard_append(serial)
                    self.status_bar.config(text=f"Скопирован серийный номер: {serial}")
                    self.log_success(f"Серийный номер скопирован: {serial}")

    def get_type_display(self, camera_type: str) -> str:
        type_map = {
            'webcam': 'USB',
            'gige': 'GigE',
            'aravis': 'GigE',
            'unknown': 'Unknown'
        }
        return type_map.get(camera_type, camera_type.upper())

    def scan_cameras(self):
        self.log_info("Запуск сканирования камер...")
        self.scan_btn.config(state=tk.DISABLED)
        self.loading_label.config(text="Поиск камер...")
        self.status_bar.config(text="Выполняется сканирование камер...")

        thread = Thread(target=self._scan_cameras_thread, daemon=True)
        thread.start()

    def _scan_cameras_thread(self):
        try:
            self.cameras = CameraScanner.scan_all()
            self.root.after(0, self._update_camera_list)
            self.log_success(
                f"Сканирование завершено. Найдено камер: {len(self.cameras)}"
            )
        except Exception as e:
            error_msg = f"Ошибка сканирования: {e}"
            self.log_error(error_msg)
            self.root.after(0, lambda: self._show_error(error_msg))

    def _update_camera_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.cameras:
            self.tree.insert('', 'end', values=('Нет камер', '—', '—', '—', '—'))
            self.status_bar.config(text="Камеры не найдены")
            self.log_warning("Камеры не найдены")
        else:
            for cam in self.cameras:
                status = cam.get('status', 'Unknown')
                if status == 'Available':
                    status_display = 'Доступна'
                elif status == 'Connected':
                    status_display = 'Подключена'
                elif status == 'Failed':
                    status_display = 'Ошибка'
                else:
                    status_display = 'Неизвестно'

                ip = cam.get('ip', 'N/A') or 'N/A'
                camera_type = cam.get('type', 'unknown')
                type_display = self.get_type_display(camera_type)

                self.tree.insert(
                    '', 'end',
                    values=(
                        cam.get('name', 'Unknown'),
                        type_display,
                        cam.get('serial', 'N/A'),
                        ip,
                        status_display
                    ),
                    tags=(camera_type,)
                )

            aravis_count = sum(1 for c in self.cameras
                               if c.get('type') in ('aravis', 'gige'))
            status_text = f"Найдено камер: {len(self.cameras)}"
            if aravis_count > 0:
                status_text += f" (GigE: {aravis_count})"

            self.status_bar.config(text=status_text)
            self.log_info(f"Отображено камер в таблице: {len(self.cameras)}")

        self.scan_btn.config(state=tk.NORMAL)
        self.loading_label.config(text="")
        self.connect_btn.config(state=tk.DISABLED)

    def on_select_camera(self, event):
        selection = self.tree.selection()
        if selection:
            selected_index = self.tree.index(selection[0])

            if selected_index < len(self.cameras):
                self.selected_camera = self.cameras[selected_index]
                self.connect_btn.config(state=tk.NORMAL)

                name = self.selected_camera.get('name', 'Unknown')
                camera_type = self.selected_camera.get('type', 'unknown')
                ip = self.selected_camera.get('ip', 'N/A')
                serial = self.selected_camera.get('serial', 'N/A')
                type_display = self.get_type_display(camera_type)

                self.status_bar.config(
                    text=f"Выбрана: {name} | Тип: {type_display} "
                         f"| SN: {serial} | IP: {ip}"
                )
                self.log_info(
                    f"Выбрана камера: {name} "
                    f"(Тип: {type_display}, SN: {serial})"
                )
            else:
                self.connect_btn.config(state=tk.DISABLED)

    def connect_camera(self):
        if not self.selected_camera:
            messagebox.showwarning("Предупреждение", "Сначала выберите камеру")
            return

        self.stop_video_stream()
        if self.is_recording:
            self.stop_recording()

        camera_name = self.selected_camera.get('name', 'Unknown')
        camera_type = self.selected_camera.get('type', 'webcam')
        device_id = self.selected_camera.get('device_id', 0)
        ip = self.selected_camera.get('ip', 'N/A')
        saved_ip = self.selected_camera.get('_saved_ip', None)

        try:
            self.log_info(f"Попытка подключения к {camera_name}...")
            self.status_bar.config(text=f"Подключение к {camera_name}...")
            self.connect_btn.config(state=tk.DISABLED)

            if camera_type in ('gige', 'aravis'):
                self.current_camera = create_camera(
                    'aravis',
                    device_id=device_id,
                    saved_ip=saved_ip,
                    pixel_format="Mono8"
                )
            else:
                self.log_error(f"Неизвестный тип камеры: {camera_type}")
                return

            self._update_camera_status(camera_name, 'Connected')

            type_display = self.get_type_display(camera_type)
            info_msg = f"Подключено к камере: {camera_name}\n"
            info_msg += f"Тип: {type_display}\n"
            info_msg += (f"Серийный номер: "
                         f"{self.selected_camera.get('serial', 'N/A')}\n")
            if ip not in ('N/A', 'Unknown'):
                info_msg += f"IP-адрес: {ip}"

            self.log_success(f"Успешно подключено к {camera_name} (IP: {ip})")
            messagebox.showinfo("Успешно", info_msg)
            self.status_bar.config(
                text=f"Подключено: {camera_name} (Тип: {type_display})"
            )

            self.show_video_btn.config(state=tk.NORMAL)
            self.video_control_btn.config(state=tk.NORMAL)
            self.snapshot_btn.config(state=tk.NORMAL)
            self.record_btn.config(state=tk.NORMAL)
            self.video_info_label.config(
                text=f"Камера: {camera_name} ({type_display})"
            )
            self.video_status.config(
                text=f"Статус: Подключено к {camera_name}"
            )

        except Exception as e:
            error_msg = f"Не удалось подключиться: {e}"
            self.log_error(error_msg)
            messagebox.showerror("Ошибка", error_msg)
            self.status_bar.config(text="Ошибка подключения")
            self.current_camera = None
        finally:
            self.connect_btn.config(state=tk.NORMAL)

    def _update_camera_status(self, camera_name, new_status):
        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            if values and values[0] == camera_name:
                new_values = list(values)
                if new_status == 'Connected':
                    new_values[4] = 'Подключена'
                elif new_status == 'Available':
                    new_values[4] = 'Доступна'
                self.tree.item(item, values=tuple(new_values))
                break

    def refresh_status(self):
        self.log_info("Обновление статуса камер...")
        self.status_bar.config(text="Обновление статуса...")
        self.scan_cameras()

    def show_video(self):
        self.notebook.select(self.video_tab)
        self.log_info("Переключение на вкладку видео")
        if not self.is_streaming:
            self.toggle_video()

    # ============ ВИДЕО ============
    def toggle_video(self):
        if self.is_streaming:
            self.stop_video_stream()
        else:
            self.start_video_stream()

    def start_video_stream(self):
        if not self.current_camera:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к камере")
            return
        if self.is_streaming:
            return

        self.user_wants_display = True
        self.display_enabled = True
        self.is_streaming = True
        self._frame_size = None

        self.video_control_btn.config(text="Остановить видео")
        self.video_status.config(text="Статус: Запуск видео...")
        self.log_info("Запуск видео потока (с отображением)...")

        self.frame_count = 0
        self.fps_start_time = time.time()
        self.current_fps = 0.0
        with self.frame_lock:
            self.last_frame_bgr = None
            self.frame_ready = False

        self.video_thread = Thread(target=self._capture_loop, daemon=True)
        self.video_thread.start()

    def stop_video_stream(self):
        was_streaming = self.is_streaming
        self.is_streaming = False
        self.display_enabled = False
        self.user_wants_display = False

        if self.canvas_image_id:
            try:
                self.video_canvas.delete(self.canvas_image_id)
            except Exception:
                pass
            self.canvas_image_id = None
        try:
            self.video_canvas.image = None
        except Exception:
            pass

        self.video_control_btn.config(text="Запустить видео")
        self.fps_label.config(text=f"FPS: {self.current_fps:.1f}")
        self.video_status.config(text="Статус: Видео остановлено")
        if was_streaming:
            self.log_info("Видео поток остановлен")

    def _capture_loop(self):
        """Фоновый поток захвата. Не трогает Tkinter."""
        while self.is_streaming and self.current_camera:
            try:
                frame = self.current_camera.get_frame()
                if frame is None:
                    continue

                # Размер кадра
                if self._frame_size is None:
                    self._frame_size = (frame.shape[1], frame.shape[0])

                # FPS
                self.frame_count += 1
                now = time.time()
                if now - self.fps_start_time >= self.fps_update_interval:
                    self.current_fps = (
                        self.frame_count / (now - self.fps_start_time)
                    )
                    self.frame_count = 0
                    self.fps_start_time = now

                # Запись (Mono8, isColor=False)
                if self.is_recording and self.video_recorder:
                    self.video_recorder.write_frame(frame)

                # Отображение: Mono8 -> BGR только для показа
                if self.display_enabled:
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

                    h, w = frame_bgr.shape[:2]
                    max_w, max_h = 960, 540
                    if w > max_w or h > max_h:
                        s = min(max_w / w, max_h / h)
                        display = cv2.resize(
                            frame_bgr, (int(w * s), int(h * s)),
                            interpolation=cv2.INTER_AREA
                        )
                    else:
                        display = frame_bgr

                    dh, dw = display.shape[:2]
                    font_scale = 0.6
                    thickness = 1

                    if self.is_recording and self.recording_start_time:
                        e = time.time() - self.recording_start_time
                        hh, mm, ss = (int(e // 3600),
                                      int((e % 3600) // 60),
                                      int(e % 60))
                        tstr = (f"{hh:02d}:{mm:02d}:{ss:02d}"
                                if hh else f"{mm:02d}:{ss:02d}")
                        cv2.putText(display, tstr, (dw - 140, 30),
                                    cv2.FONT_HERSHEY_SIMPLEX, font_scale,
                                    (0, 255, 0), thickness, cv2.LINE_AA)

                    cv2.putText(display, f"FPS: {self.current_fps:.1f}",
                                (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, font_scale,
                                (0, 255, 255), thickness, cv2.LINE_AA)

                    with self.frame_lock:
                        self.last_frame_bgr = display
                        self.frame_ready = True

            except Exception as e:
                if self.is_streaming:
                    print(f"Ошибка захвата: {e}")
                break

    def update_ui_loop(self):
        """Main-поток. Единственное место работы с Tkinter."""
        try:
            if self.is_streaming and self.display_enabled and self.frame_ready:
                with self.frame_lock:
                    frame_bgr = self.last_frame_bgr
                    self.frame_ready = False

                if frame_bgr is not None:
                    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame_rgb)
                    imgtk = ImageTk.PhotoImage(image=img)

                    if self.canvas_image_id:
                        self.video_canvas.delete(self.canvas_image_id)

                    cw = self.video_canvas.winfo_width()
                    ch = self.video_canvas.winfo_height()
                    iw, ih = imgtk.width(), imgtk.height()

                    x = max(0, (cw - iw) // 2) if cw > 1 else 0
                    y = max(0, (ch - ih) // 2) if ch > 1 else 0

                    self.canvas_image_id = self.video_canvas.create_image(
                        x, y, anchor=tk.NW, image=imgtk
                    )
                    self.video_canvas.image = imgtk

                    self.video_status.config(text="Статус: Видео идет")

            if self.is_streaming:
                self.fps_label.config(text=f"FPS: {self.current_fps:.1f}")

        except Exception as e:
            print(f"UI loop error: {e}")

        self.root.after(66, self.update_ui_loop)

    # ============ ЗАПИСЬ ============
    def save_video(self):
        return filedialog.askdirectory()

    def toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not self.current_camera:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к камере")
            return
        if self.is_recording:
            return

        try:
            recordings_dir = self.save_video()
            if not recordings_dir:
                return

            camera_info = self.current_camera.get_info()
            camera_name = camera_info.get('name', 'camera')

            # Гасим отображение на время записи
            self.display_enabled = False
            if self.canvas_image_id:
                self.video_canvas.delete(self.canvas_image_id)
                self.canvas_image_id = None
            try:
                self.video_canvas.image = None
            except Exception:
                pass
            self.log_info("Отображение отключено на время записи")

            # Если захват не идёт — запускаем без показа
            if not self.is_streaming:
                self.is_streaming = True
                self._frame_size = None
                self.frame_count = 0
                self.fps_start_time = time.time()
                self.current_fps = 0.0
                with self.frame_lock:
                    self.last_frame_bgr = None
                    self.frame_ready = False
                self.video_thread = Thread(target=self._capture_loop, daemon=True)
                self.video_thread.start()
                self.video_control_btn.config(text="Остановить видео")
                self.log_info("Захват запущен (без отображения)")

            # Ждём появления размера кадра
            self.video_status.config(text="Статус: Инициализация записи...")
            self.root.update_idletasks()

            start_wait = time.time()
            while self._frame_size is None and (time.time() - start_wait) < 3.0:
                time.sleep(0.02)

            if self._frame_size is None:
                self.log_error("Не удалось определить размер кадра")
                messagebox.showerror("Ошибка", "Камера не отдаёт кадры")
                return

            width, height = self._frame_size
            self.log_info(f"Разрешение кадра: {width}x{height}")

            # FPS: измеренный, БЕЗ округления
            target_fps = int(self.current_fps) if self.current_fps >= 5 else 30
            self.log_info(
                f"FPS записи: {target_fps} (измеренный {self.current_fps:.1f})"
            )

            self.video_recorder = VideoRecorder(
                output_dir=recordings_dir,
                fps=target_fps,
                is_color=False   # Mono8
            )

            self.current_record_path = self.video_recorder.start_recording(
                width, height, camera_name
            )

            self.recording_start_time = time.time()
            self.recording_duration = 0
            self.is_recording = True
            self.record_btn.config(text="Остановить запись")
            self.recording_label.config(text="Запись: ИДЕТ", foreground='#ff0000')
            self.log_success(f"Запись начата: {self.current_record_path}")
            self.video_status.config(
                text="Статус: Запись идет (отображение выкл.)"
            )

        except Exception as e:
            self.log_error(f"Ошибка начала записи: {e}")
            messagebox.showerror("Ошибка", f"Не удалось начать запись: {e}")

    def stop_recording(self):
        if not self.is_recording or not self.video_recorder:
            return
        try:
            saved_path = self.video_recorder.stop_recording()
            self.is_recording = False
            self.recording_start_time = None
            self.recording_duration = 0
            self.video_recorder = None
            self.record_btn.config(text="Записать")
            self.recording_label.config(text="Запись: Нет", foreground="#3010c2")

            # Возвращаем отображение, если пользователь его включал
            if self.user_wants_display and self.is_streaming:
                self.display_enabled = True
                self.log_info("Отображение снова включено")
            else:
                self.display_enabled = False

            if saved_path:
                self.log_success(f"Запись сохранена: {saved_path}")
                self.video_status.config(
                    text=f"Статус: Запись сохранена: "
                         f"{os.path.basename(saved_path)}"
                )
                if messagebox.askyesno(
                    "Запись завершена",
                    f"Видео сохранено в:\n{saved_path}\n\nОткрыть папку?"
                ):
                    dir_path = os.path.dirname(saved_path)
                    if platform.system() == 'Windows':
                        os.startfile(dir_path)
                    elif platform.system() == 'Darwin':
                        subprocess.Popen(['open', dir_path])
                    else:
                        subprocess.Popen(['xdg-open', dir_path])
            else:
                self.log_warning("Запись не была сохранена")
                self.video_status.config(text="Статус: Запись не сохранена")

        except Exception as e:
            self.log_error(f"Ошибка остановки записи: {e}")
            messagebox.showerror("Ошибка", f"Не удалось остановить запись: {e}")
            self.is_recording = False
            self.recording_start_time = None
            self.video_recorder = None
            self.record_btn.config(text="Записать")
            self.recording_label.config(
                text="Запись: Нет", foreground="#3010c2"
            )

    # ============ СНИМОК ============
    def save_snapshot(self):
        return filedialog.askdirectory()

    def take_snapshot(self):
        if not self.current_camera:
            messagebox.showwarning("Предупреждение", "Камера не подключена")
            return
        try:
            frame = self.current_camera.get_frame()
            if frame is None:
                self.log_warning("Не удалось получить кадр для снимка")
                messagebox.showwarning("Предупреждение", "Не удалось получить кадр")
                return

            snapshots_dir = self.save_snapshot()
            if not snapshots_dir:
                return

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            camera_name = self.current_camera.get_info().get('name', 'camera')
            safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
            filename = f"{safe_name}_{timestamp}.png"
            filepath = os.path.join(snapshots_dir, filename)

            # Mono8 PNG
            cv2.imwrite(filepath, frame)
            self.log_success(f"Снимок сохранен: {filepath}")

            if messagebox.askyesno(
                "Снимок сохранен",
                f"Снимок сохранен в:\n{filepath}\n\nОткрыть папку?"
            ):
                if platform.system() == 'Windows':
                    os.startfile(snapshots_dir)
                elif platform.system() == 'Darwin':
                    subprocess.Popen(['open', snapshots_dir])
                else:
                    subprocess.Popen(['xdg-open', snapshots_dir])
        except Exception as e:
            self.log_error(f"Ошибка при сохранении снимка: {e}")
            messagebox.showerror("Ошибка", f"Не удалось сохранить снимок: {e}")

    def _show_error(self, error_message):
        self.scan_btn.config(state=tk.NORMAL)
        self.loading_label.config(text="")
        self.status_bar.config(text=f"Ошибка: {error_message}")
        messagebox.showerror("Ошибка", error_message)


def main():
    root = tk.Tk()
    app = CameraDiscoveryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()