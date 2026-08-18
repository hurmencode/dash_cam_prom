import re

import cv2
from datetime import datetime
import os
from PIL import Image, ImageTk
import sys
import time
import tkinter as tk
from tkinter import PhotoImage, ttk, messagebox, filedialog
from threading import Thread, Lock

# Добавляем путь к бэкенду
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Импортируем из бэкенда
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
        
        # Переменные для FPS
        self.frame_count = 0
        self.fps_start_time = time.time()
        self.current_fps = 0
        self.fps_update_interval = 0.3
        
        # Буфер для последнего кадра
        self.last_frame = None
        self.frame_lock = Lock()
        self.frame_ready = False
        
        # Переменные для записи видео
        self.video_recorder = None
        self.is_recording = False
        self.recording_thread = None
        self.current_record_path = None
        self.osd_enabled = True
        self.show_recording_indicator = True
        
        # Создаем интерфейс
        self.create_widgets()
        
        # Перенаправляем вывод
        self.redirect_output()
        
        # Автоматический поиск
        self.root.after(500, self.scan_cameras)
        
        # Запускаем цикл обновления UI
        self.update_ui_loop()
        
        # Обработчик закрытия
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def on_closing(self):
        self.is_streaming = False
        if self.is_recording:
            self.stop_recording()
        self.root.destroy()
    
    def redirect_output(self):
        class TextRedirector:
            def __init__(self, widget, tag="stdout"):
                self.widget = widget
                self.tag = tag
            
            def write(self, str):
                if '[ERROR]' in str or 'ERROR' in str or 'Error' in str:
                    color = 'red'
                elif '[WARN]' in str or 'WARNING' in str or 'Warning' in str:
                    color = 'orange'
                elif '[SUCCESS]' in str or 'Connected' in str:
                    color = 'green'
                elif '[INFO]' in str:
                    color = 'blue'
                else:
                    color = 'white'
                
                if str.strip():
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    self.widget.insert(tk.END, f"[{timestamp}] {str}", (color,))
                    self.widget.see(tk.END)
                    self.widget.update()
            
            def flush(self):
                pass
        
        sys.stdout = TextRedirector(self.log_text, "stdout")
        sys.stderr = TextRedirector(self.log_text, "stderr")
    
    def create_widgets(self):
        # Создаем главный Notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Вкладка 1: Камеры
        self.camera_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.camera_tab, text="Камеры")
        self.create_camera_tab()
        
        # Вкладка 2: Видео
        self.video_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.video_tab, text="Видео")
        self.create_video_tab()
        
        # Вкладка 3: Логи
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.log_tab, text="Логи")
        self.create_log_tab()
    
    def create_camera_tab(self):
        top_frame = ttk.Frame(self.camera_tab, padding="10")
        top_frame.pack(fill=tk.X)
        
        self.scan_btn = ttk.Button(
            top_frame, 
            text="Поиск камер", 
            command=self.scan_cameras,
            width=20
        )
        self.scan_btn.pack(side=tk.LEFT, padx=5)
        
        self.connect_btn = ttk.Button(
            top_frame,
            text="Подключиться",
            command=self.connect_camera,
            width=20,
            state=tk.DISABLED
        )
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.show_video_btn = ttk.Button(
            top_frame,
            text="Показать видео",
            command=self.show_video,
            width=20,
            state=tk.DISABLED
        )
        self.show_video_btn.pack(side=tk.LEFT, padx=5)
        
        self.refresh_btn = ttk.Button(
            top_frame,
            text="Обновить статус",
            command=self.refresh_status,
            width=20
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=5)
        
        self.loading_label = ttk.Label(top_frame, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)
        
        columns = ('name', 'type', 'serial', 'ip', 'status')
        self.tree = ttk.Treeview(
            self.camera_tab, 
            columns=columns, 
            show='headings',
            height=15
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
        
        self.tree.column('name', anchor='center')
        self.tree.column('type', anchor='center')
        self.tree.column('serial', anchor='center')
        self.tree.column('ip', anchor='center')
        self.tree.column('status', anchor='center')
        
        scrollbar = ttk.Scrollbar(self.camera_tab, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=10)
        
        self.tree.bind('<<TreeviewSelect>>', self.on_select_camera)
        self.create_context_menu()
        
    
    def create_video_tab(self):
        # Верхняя панель управления
        controls_frame = ttk.Frame(self.video_tab, padding="10")
        controls_frame.pack(fill=tk.X)
        
        # Информация о камере
        self.video_info_label = ttk.Label(
            controls_frame,
            text="Камера не выбрана",
            font=('Arial', 10)
        )
        self.video_info_label.pack(side=tk.LEFT, padx=5)
        
        # Информация о FPS
        self.fps_label = ttk.Label(
            controls_frame,
            text="FPS: 0 | Родной: 0",
            font=('Arial', 10, 'bold'),
            foreground='#00ff00'
        )
        self.fps_label.pack(side=tk.LEFT, padx=20)
        
        # Информация о записи
        self.recording_label = ttk.Label(
            controls_frame,
            text="Запись: Нет",
            font=('Arial', 10, 'bold'),
            foreground="#3010c2"
        )
        self.recording_label.pack(side=tk.LEFT, padx=20)
        
        # ============ Выбор режима записи ============
        mode_frame = ttk.Frame(controls_frame)
        mode_frame.pack(side=tk.LEFT, padx=10)
        
        ttk.Label(mode_frame, text="Режим:").pack(side=tk.LEFT)
        
        self.record_mode_var = tk.StringVar(value="Сжатый (XVID)")
        mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.record_mode_var,
            values=[
                'Сжатый (XVID)', 
                'Без потерь (HFYU)', 
                'Высокое качество (сжатый) (MJPG)',
                'Кадры PNG (без потерь)'
            ],
            state='readonly',
            width=35
        )
        mode_combo.pack(side=tk.LEFT, padx=5)
        
        # ===================================================
        
        # Кнопка запуска/остановки видео
        self.video_control_btn = ttk.Button(
            controls_frame,
            text="Запустить видео",
            command=self.toggle_video,
            width=18,
            state=tk.DISABLED
        )
        self.video_control_btn.pack(side=tk.RIGHT, padx=5)
        
        # Кнопка записи видео
        self.record_btn = ttk.Button(
            controls_frame,
            text="Записать",
            command=self.toggle_recording,
            width=15,
            state=tk.DISABLED
        )
        self.record_btn.pack(side=tk.RIGHT, padx=5)
        
        # Кнопка сохранения кадра
        self.snapshot_btn = ttk.Button(
            controls_frame,
            text="Снимок",
            command=self.take_snapshot,
            width=12,
            state=tk.DISABLED
        )
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)
        
        # Фрейм для видео
        self.video_frame = ttk.Frame(self.video_tab, relief=tk.SUNKEN, borderwidth=2)
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Канвас для видео
        self.video_canvas = tk.Canvas(self.video_frame, background='black', highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas_image_id = None
        
        # Статус видео
        self.video_status = ttk.Label(
            self.video_tab,
            text="Статус: Ожидание",
            relief=tk.SUNKEN,
            anchor=tk.W
        )
        self.video_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
    
    def create_log_tab(self):
        log_controls = ttk.Frame(self.log_tab, padding="5")
        log_controls.pack(fill=tk.X)
        
        clear_log_btn = ttk.Button(
            log_controls,
            text="Очистить логи",
            command=self.clear_logs
        )
        clear_log_btn.pack(side=tk.LEFT, padx=5)
        
        save_log_btn = ttk.Button(
            log_controls,
            text="Сохранить логи",
            command=self.save_logs
        )
        save_log_btn.pack(side=tk.LEFT, padx=5)
        
        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            bg='#1e1e1e',
            fg='white',
            font=('Consolas', 10),
            height=20
        )
        
        self.log_text.tag_config('red', foreground='#ff6b6b')
        self.log_text.tag_config('orange', foreground='#ffa94d')
        self.log_text.tag_config('green', foreground='#69db7c')
        self.log_text.tag_config('blue', foreground='#74c0fc')
        self.log_text.tag_config('white', foreground='#ffffff')
        
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.log_info("Логи запущены. Приложение готово к работе.")
        self.log_info("Нажмите 'Поиск камер' для сканирования.")
    
    # ============ МЕТОДЫ ЛОГИРОВАНИЯ ============
    
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
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
            title="Сохранить логи"
        )
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.get(1.0, tk.END))
                self.log_success(f"Логи сохранены в {file_path}")
            except Exception as e:
                self.log_error(f"Не удалось сохранить логи: {e}")
    
    # ============ МЕТОДЫ РАБОТЫ С КАМЕРАМИ ============
    
    def create_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Копировать IP", command=self.copy_ip)
        self.context_menu.add_command(label="Копировать серийный номер", command=self.copy_serial)
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
                if ip and ip != 'N/A' and ip != 'Unknown':
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
                if serial and serial != 'N/A' and serial != 'Unknown':
                    self.root.clipboard_clear()
                    self.root.clipboard_append(serial)
                    self.status_bar.config(text=f"Скопирован серийный номер: {serial}")
                    self.log_success(f"Серийный номер скопирован: {serial}")
    
    def get_type_display(self, camera_type: str) -> str:
        type_map = {
            'webcam': 'USB',
            'lucid': 'GigE',
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
            self.log_success(f"Сканирование завершено. Найдено камер: {len(self.cameras)}")
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
                
                ip = cam.get('ip', 'N/A')
                if not ip or ip == '':
                    ip = 'N/A'
                
                camera_type = cam.get('type', 'unknown')
                type_display = self.get_type_display(camera_type)
                
                self.tree.insert(
                    '',
                    'end',
                    values=(
                        cam.get('name', 'Unknown'),
                        type_display,
                        cam.get('serial', 'N/A'),
                        ip,
                        status_display
                    ),
                    tags=(camera_type,)
                )
            
            webcam_count = sum(1 for c in self.cameras if c.get('type') == 'webcam')
            lucid_count = sum(1 for c in self.cameras if c.get('type') == 'lucid')
            
            status_text = f"Найдено камер: {len(self.cameras)}"
            if webcam_count > 0:
                status_text += f" (USB: {webcam_count}"
            if lucid_count > 0:
                status_text += f", GigE: {lucid_count}"
            if webcam_count > 0 or lucid_count > 0:
                status_text += ")"
            
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
                    text=f"Выбрана: {name} | Тип: {type_display} | SN: {serial} | IP: {ip}"
                )
                self.log_info(f"Выбрана камера: {name} (Тип: {type_display}, SN: {serial})")
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
            
            if camera_type == 'webcam':
                self.current_camera = create_camera('webcam', device_id=device_id)
                self.log_info(f"Создан объект WebcamManager с ID {device_id}")
            elif camera_type == 'lucid':
                identifier = self.selected_camera.get('ip', None)
                if identifier == 'Unknown' or identifier == 'N/A':
                    identifier = None
                self.current_camera = create_camera(
                    'lucid',
                    identifier=identifier,
                    pixel_format="Mono8"
                )
            else:
                self.log_error(f"Неизвестный тип камеры: {camera_type}")
                messagebox.showerror("Ошибка", f"Неизвестный тип камеры: {camera_type}")
                return
            
            self._update_camera_status(camera_name, 'Connected')
            
            type_display = self.get_type_display(camera_type)
            info_msg = f"Подключено к камере: {camera_name}\n"
            info_msg += f"Тип: {type_display}\n"
            info_msg += f"Серийный номер: {self.selected_camera.get('serial', 'N/A')}\n"
            if ip != 'N/A' and ip != 'Unknown':
                info_msg += f"IP-адрес: {ip}"
            
            self.log_success(f"Успешно подключено к {camera_name} (IP: {ip})")
            messagebox.showinfo("Успешно", info_msg)
            self.status_bar.config(text=f"Подключено: {camera_name} (Тип: {type_display})")
            
            self.show_video_btn.config(state=tk.NORMAL)
            self.video_control_btn.config(state=tk.NORMAL)
            self.snapshot_btn.config(state=tk.NORMAL)
            self.record_btn.config(state=tk.NORMAL)
            self.video_info_label.config(text=f"Камера: {camera_name} ({type_display})")
            self.video_status.config(text=f"Статус: Подключено к {camera_name}")
            
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
    
    # ============ МЕТОДЫ ВИДЕО ============
    
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
        
        self.is_streaming = True
        self.video_control_btn.config(text="Остановить видео")
        self.video_status.config(text="Статус: Запуск видео...")
        self.log_info("Запуск видео потока...")
        
        self.frame_count = 0
        self.fps_start_time = time.time()
        self.current_fps = 0
        
        with self.frame_lock:
            self.last_frame = None
            self.frame_ready = False
        
        self.video_thread = Thread(target=self._capture_loop, daemon=True)
        self.video_thread.start()
    
    def stop_video_stream(self):
        self.is_streaming = False
        self.video_control_btn.config(text="Запустить видео")
        
        if self.canvas_image_id:
            self.video_canvas.delete(self.canvas_image_id)
            self.canvas_image_id = None
        
        self.fps_label.config(text=f"FPS: {self.current_fps}")
        self.video_status.config(text="Статус: Видео остановлено")
        self.log_info("Видео поток остановлен")
    
    def _capture_loop(self):
        while self.is_streaming and self.current_camera:
            try:
                frame = self.current_camera.get_frame()
                
                if frame is not None:
                    # ============ НАЛОЖЕНИЕ OSD ============
                    # Создаем копию кадра для OSD
                    display_frame = frame.copy()
                    
                    # Получаем размеры
                    height, width = display_frame.shape[:2]
                    
                    # Размер шрифта в зависимости от разрешения
                    font_scale = width / 800
                    font_thickness = max(1, int(font_scale * 2))
                    
                    if self.is_recording:
                        # ===== ДЛИТЕЛЬНОСТЬ ЗАПИСИ =====
                        if self.recording_start_time:
                            elapsed = time.time() - self.recording_start_time
                            self.recording_duration = elapsed
                            
                            # Форматируем время: HH:MM:SS
                            hours = int(elapsed // 3600)
                            minutes = int((elapsed % 3600) // 60)
                            seconds = int(elapsed % 60)
                            
                            if hours > 0:
                                time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                            else:
                                time_str = f"{minutes:02d}:{seconds:02d}"
                            
                            # Позиция: справа сверху, под FPS
                            time_pos = (width - int(150 * font_scale), int(40 * font_scale))
                            
                            # Текст времени
                            cv2.putText(display_frame, time_str, time_pos, 
                                    cv2.FONT_HERSHEY_SIMPLEX, font_scale * 0.7, 
                                    (0, 255, 0), font_thickness, cv2.LINE_AA)
                    # =====  FPS (всегда) =====
                    fps_text = f"FPS: {self.current_fps:.1f}"
                    fps_pos = (width - int(200 * font_scale), height - int(30 * font_scale))
                    
                    (text_w, text_h), _ = cv2.getTextSize(fps_text, cv2.FONT_HERSHEY_SIMPLEX, 
                                                        font_scale * 0.5, font_thickness - 1)
                    bg_rect = (fps_pos[0] - 10, fps_pos[1] - text_h - 10, 
                            text_w + 20, text_h + 20)
                    cv2.rectangle(display_frame, bg_rect, (0, 0, 0, 150), -1)
                    
                    cv2.putText(display_frame, fps_text, fps_pos, 
                            cv2.FONT_HERSHEY_SIMPLEX, font_scale * 0.5, 
                            (0, 255, 255), font_thickness - 1, cv2.LINE_AA)
                    
                    # ========================================
                    
                    # Обновляем счетчик FPS
                    self.frame_count += 1
                    
                    current_time = time.time()
                    elapsed = current_time - self.fps_start_time
                    
                    if elapsed >= self.fps_update_interval:
                        self.current_fps = self.frame_count / elapsed
                        self.frame_count = 0
                        self.fps_start_time = current_time
                        self.root.after(0, self._update_fps_display)
                    
                    # Если идет запись - сохраняем кадр (оригинал, без OSD)
                    if self.is_recording and self.video_recorder:
                        self.video_recorder.write_frame(frame)  # Сохраняем оригинал без OSD
                    
                    # Конвертируем для отображения (с OSD)
                    frame_rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
                    
                    height, width = frame_rgb.shape[:2]
                    max_width = self.video_frame.winfo_width() or 1024
                    max_height = self.video_frame.winfo_height() or 768
                    
                    if width > max_width or height > max_height:
                        scale = min(max_width/width, max_height/height)
                        new_width = int(width * scale)
                        new_height = int(height * scale)
                        if new_width > 0 and new_height > 0:
                            frame_rgb = cv2.resize(frame_rgb, (new_width, new_height), 
                                                interpolation=cv2.INTER_LINEAR)
                    
                    img = Image.fromarray(frame_rgb)
                    imgtk = ImageTk.PhotoImage(image=img)
                    
                    with self.frame_lock:
                        self.last_frame = imgtk
                        self.frame_ready = True
                    
                else:
                    time.sleep(0.001)
                    
            except Exception as e:
                if self.is_streaming:
                    self.log_error(f"Ошибка в захвате: {e}")
                break
    
    def update_ui_loop(self):
        if self.is_streaming and self.frame_ready:
            with self.frame_lock:
                if self.last_frame is not None:
                    if self.canvas_image_id:
                        self.video_canvas.delete(self.canvas_image_id)
                    
                    img_width = self.last_frame.width()
                    img_height = self.last_frame.height()
                    
                    canvas_width = self.video_canvas.winfo_width()
                    canvas_height = self.video_canvas.winfo_height()
                    
                    if canvas_width > 1 and canvas_height > 1:
                        x = (canvas_width - img_width) // 2
                        y = (canvas_height - img_height) // 2
                        self.canvas_image_id = self.video_canvas.create_image(x, y, 
                                                                             anchor=tk.NW, 
                                                                             image=self.last_frame)
                    else:
                        self.canvas_image_id = self.video_canvas.create_image(0, 0, 
                                                                             anchor=tk.NW, 
                                                                             image=self.last_frame)
                    
                    self.video_canvas.image = self.last_frame
                    self.frame_ready = False
                    self.video_status.config(text="Статус: Видео идет")
        
        self.root.after(1, self.update_ui_loop)
    
    def _update_fps_display(self):
            fps_text = f"FPS: {self.current_fps:.1f}"
            
            self.fps_label.config(text=fps_text)
    
    # ============ МЕТОДЫ ЗАПИСИ ВИДЕО ============

    def save_video(self):
        file_path = filedialog.askdirectory()
        return file_path

    
    def toggle_recording(self):
        """Запускает или останавливает запись видео"""
        if not self.is_streaming:
            messagebox.showwarning("Предупреждение", "Сначала запустите видео")
            return
        
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()
    
    def start_recording(self):
        """Начинает запись видео"""
        if not self.current_camera or not self.is_streaming:
            return
        
        if self.is_recording:
            return
        
        try:
            # Создаем папку для записей
            # recordings_dir = os.path.join(os.path.dirname(__file__), 'recordings')
            # os.makedirs(recordings_dir, exist_ok=True)

            recordings_dir = self.save_video()
            
            # Получаем информацию о камере
            camera_info = self.current_camera.get_info()
            camera_name = camera_info.get('name', 'camera')
            
            # ============ ПОЛУЧАЕМ РАЗМЕР КАДРА ============
            # Пробуем получить кадр несколько раз
            frame = None
            for attempt in range(5):
                frame = self.current_camera.get_frame()
                if frame is not None:
                    break
                time.sleep(0.1)
                self.log_info(f"Попытка получения кадра {attempt + 1}/5...")
            
            if frame is None:
                # Если не удалось получить кадр, используем разрешение из настроек
                if hasattr(self.current_camera, '_width') and hasattr(self.current_camera, '_height'):
                    height = self.current_camera._height
                    width = self.current_camera._width
                    self.log_info(f"Используем разрешение из настроек: {width}x{height}\n")
                else:
                    self.log_error("Не удалось получить кадр для определения размера\n")
                    return
            else:
                height, width = frame.shape[:2]
                self.log_info(f"Разрешение кадра: {width}x{height}")
            
            # ============ ВЫБОР РЕЖИМА ЗАПИСИ ============
            mode_map = {
                'Сжатый (XVID)': 'lossy',
                'Без потерь (HFYU)': 'lossless',
                'Высокое качество (сжатый) (MJPG)': 'high_quality',
                'Кадры PNG (без потерь)': 'png_frames'
            }
            
            selected_mode = self.record_mode_var.get()
            record_mode = mode_map.get(selected_mode, 'lossy')
            
            self.log_info(f"Режим записи: {selected_mode}")
            
            # Создаем рекордер
            self.video_recorder = VideoRecorder(
                output_dir=recordings_dir,
                fps=int(self.current_fps),
                mode=record_mode
            )
            
            # Если выбран режим PNG - сохраняем каждый кадр как PNG
            if record_mode == 'png_frames':
                self.video_recorder.save_frames_as_png = True
                self.log_info("Включено сохранение кадров в PNG\n")
            
            # Запускаем запись
            self.current_record_path = self.video_recorder.start_recording(
                width, height, camera_name
            )
            
            # ============ УСТАНАВЛИВАЕМ ВРЕМЯ НАЧАЛА ЗАПИСИ ============
            self.recording_start_time = time.time()
            self.recording_duration = 0
            # ===========================================================
            
            self.is_recording = True
            self.record_btn.config(text="Остановить запись")
            self.recording_label.config(text="Запись: ИДЕТ", foreground='#ff0000')
            
            # Показываем режим в статусе
            mode_display = selected_mode
            self.log_success(f"Запись начата: {self.current_record_path}")
            self.log_info(f"Режим: {mode_display}")
            self.video_status.config(text=f"Статус: Запись идет ({mode_display})...")
            
        except Exception as e:
            self.log_error(f"Ошибка начала записи: {e}\n")
            messagebox.showerror("Ошибка", f"Не удалось начать запись: {e}\n")
    
    def stop_recording(self):
        """Останавливает запись видео"""
        if not self.is_recording or not self.video_recorder:
            return
        
        try:
            saved_path = self.video_recorder.stop_recording()
            self.is_recording = False
            
            # ============ СБРАСЫВАЕМ ТАЙМЕР ============
            self.recording_start_time = None
            self.recording_duration = 0
            # ===========================================
            
            self.video_recorder = None
            self.record_btn.config(text="Записать")
            self.recording_label.config(text="Запись: Нет", foreground="#3010c2")
            
            if saved_path:
                self.log_success(f"Запись сохранена: {saved_path}")
                self.video_status.config(text=f"Статус: Запись сохранена: {os.path.basename(saved_path)}")
                
                if messagebox.askyesno("Запись завершена", 
                                    f"Видео сохранено в:\n{saved_path}\n\nОткрыть папку?"):
                    os.startfile(os.path.dirname(saved_path))
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
            self.recording_label.config(text="Запись: Нет", foreground="#3010c2")
    
    def save_snapshot(self):
        file_path = filedialog.askdirectory()
        return file_path

    def take_snapshot(self):
        if not self.is_streaming or not self.current_camera:
            messagebox.showwarning("Предупреждение", "Видео не запущено")
            return
        
        try:
            frame = self.current_camera.get_frame()
            
            if frame is not None:
                snapshots_dir = self.save_snapshot()
                
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                camera_name = self.current_camera.get_info().get('name', 'camera')
                safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
                filename = f"{safe_name}_{timestamp}.png"
                filepath = os.path.join(snapshots_dir, filename)
                
                cv2.imwrite(filepath, frame)
                self.log_success(f"Снимок сохранен: {filepath}")
                
                if messagebox.askyesno("Снимок сохранен", 
                                       f"Снимок сохранен в:\n{filepath}\n\nОткрыть папку?"):
                    os.startfile(snapshots_dir)
            else:
                self.log_warning("Не удалось получить кадр для снимка")
                messagebox.showwarning("Предупреждение", "Не удалось получить кадр")
                
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