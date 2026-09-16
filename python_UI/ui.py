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

# Импортируем очищенные компоненты из бэкенда
from backend.camera_manager import CameraScanner, create_camera, VideoRecorder

class CameraDiscoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Интерфейс управления Lucid Triton GigE")

        try:
            icon = PhotoImage(file="favicon.png")
            self.root.iconphoto(False, icon)
        except Exception as e:
            print(f"Не удалось загрузить иконку: {e}")

        self.root.geometry("1300x700")
        
        # Переменные состояния
        self.cameras = []
        self.selected_camera = None
        self.current_camera = None
        self.is_streaming = False
        self.video_thread = None
        
        # Переменные для расчета реального FPS с камеры
        self.frame_count = 0
        self.fps_start_time = time.time()
        self.current_fps = 0
        self.fps_update_interval = 0.3
        
        # Буфер для отрисовки кадров в GUI
        self.last_frame = None
        self.frame_lock = Lock()
        self.frame_ready = False
        
        # Переменные для записи видео
        self.video_recorder = None
        self.is_recording = False
        self.recording_start_time = None
        self.current_record_path = None
        
        # Отрисовка интерфейса
        self.create_widgets()
        
        # Перенаправление стандартного вывода в окно логов программы
        self.redirect_output()
        
        # Автоматический поиск камер при старте
        self.root.after(500, self.scan_cameras)
        
        # Запуск бесконечного цикла обновления UI кадров
        self.update_ui_loop()
        
        # Обработчик корректного закрытия окна приложения
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def on_closing(self):
        self.is_streaming = False
        if self.is_recording:
            self.stop_recording()
        if self.current_camera:
            self.current_camera.release()
        self.root.destroy()
    
    def redirect_output(self):
        class TextRedirector:
            def __init__(self, widget, tag="stdout"):
                self.widget = widget
                self.tag = tag
            
            def write(self, string_data):
                if '[ERROR]' in string_data or 'ERROR' in string_data or 'Error' in string_data:
                    color = 'red'
                elif '[WARN]' in string_data or 'WARNING' in string_data or 'Warning' in string_data:
                    color = 'orange'
                elif '[SUCCESS]' in string_data or 'Connected' in string_data:
                    color = 'green'
                elif '[INFO]' in string_data:
                    color = 'blue'
                else:
                    color = 'white'
                
                if string_data.strip():
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    self.widget.insert(tk.END, f"[{timestamp}] {string_data}", (color,))
                    self.widget.see(tk.END)
                    self.widget.update()
            
            def flush(self):
                pass
        
        sys.stdout = TextRedirector(self.log_text, "stdout")
        sys.stderr = TextRedirector(self.log_text, "stderr")
    
    def create_widgets(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Вкладка 1: Список доступных камер
        self.camera_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.camera_tab, text="Камеры GigE")
        self.create_camera_tab()
        
        # Вкладка 2: Просмотр видеопотока и управление захватом
        self.video_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.video_tab, text="Видео")
        self.create_video_tab()
        
        # Вкладка 3: Консоль вывода логов
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.log_tab, text="Логи")
        self.create_log_tab()
    
    def create_camera_tab(self):
        top_frame = ttk.Frame(self.camera_tab, padding="10")
        top_frame.pack(fill=tk.X)
        
        self.scan_btn = ttk.Button(top_frame, text="Поиск GigE камер", command=self.scan_cameras, width=20)
        self.scan_btn.pack(side=tk.LEFT, padx=5)
        
        self.connect_btn = ttk.Button(top_frame, text="Подключиться", command=self.connect_camera, width=20, state=tk.DISABLED)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.show_video_btn = ttk.Button(top_frame, text="Показать видео", command=self.show_video, width=20, state=tk.DISABLED)
        self.show_video_btn.pack(side=tk.LEFT, padx=5)
        
        self.refresh_btn = ttk.Button(top_frame, text="Обновить статус", command=self.refresh_status, width=20)
        self.refresh_btn.pack(side=tk.LEFT, padx=5)
        
        self.loading_label = ttk.Label(top_frame, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)
        
        columns = ('name', 'type', 'serial', 'ip', 'status')
        self.tree = ttk.Treeview(self.camera_tab, columns=columns, show='headings', height=15)

        self.status_bar = ttk.Label(self.camera_tab, text="Готов к работе. Нажмите 'Поиск GigE камер'", relief=tk.SUNKEN)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
        
        self.tree.heading('name', text='Имя камеры')
        self.tree.heading('type', text='Интерфейс')
        self.tree.heading('serial', text='Серийный номер')
        self.tree.heading('ip', text='IP-адрес')
        self.tree.heading('status', text='Статус')
        
        self.tree.column('name', anchor='center')
        self.tree.column('type', anchor='center', width=100)
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
        controls_frame = ttk.Frame(self.video_tab, padding="10")
        controls_frame.pack(fill=tk.X)
        
        self.video_info_label = ttk.Label(controls_frame, text="Камера не выбрана", font=('Arial', 10))
        self.video_info_label.pack(side=tk.LEFT, padx=5)
        
        self.fps_label = ttk.Label(controls_frame, text="FPS: 0", font=('Arial', 10, 'bold'), foreground="#000000")
        self.fps_label.pack(side=tk.LEFT, padx=20)
        
        self.recording_label = ttk.Label(controls_frame, text="Запись: Нет", font=('Arial', 10, 'bold'), foreground="#3010c2")
        self.recording_label.pack(side=tk.LEFT, padx=20)
        
        mode_frame = ttk.Frame(controls_frame)
        mode_frame.pack(side=tk.LEFT, padx=10)
        
        ttk.Label(mode_frame, text="Режим кодирования:").pack(side=tk.LEFT)
        
        self.record_mode_var = tk.StringVar(value="Сжатый (XVID)")
        mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.record_mode_var,
            values=[
                'Сжатый (XVID)', 
                'Без потерь (HFYU)', 
                'Высокое качество (MJPG)',
                'Кадры PNG (без потерь)'
            ],
            state='readonly',
            width=30
        )
        mode_combo.pack(side=tk.LEFT, padx=5)
        
        self.video_control_btn = ttk.Button(controls_frame, text="Запустить видео", command=self.toggle_video, width=18, state=tk.DISABLED)
        self.video_control_btn.pack(side=tk.RIGHT, padx=5)
        
        self.record_btn = ttk.Button(controls_frame, text="Записать видео", command=self.toggle_recording, width=18, state=tk.DISABLED)
        self.record_btn.pack(side=tk.RIGHT, padx=5)
        
        self.snapshot_btn = ttk.Button(controls_frame, text="Снимок экрана", command=self.take_snapshot, width=15, state=tk.DISABLED)
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)
        
        self.video_frame = ttk.Frame(self.video_tab, relief=tk.SUNKEN, borderwidth=2)
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        self.video_canvas = tk.Canvas(self.video_frame, background='black', highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas_image_id = None
        
        self.video_status = ttk.Label(self.video_tab, text="Статус: Ожидание действия", relief=tk.SUNKEN, anchor=tk.W)
        self.video_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

    def create_log_tab(self):
        log_controls = ttk.Frame(self.log_tab, padding="5")
        log_controls.pack(fill=tk.X)
        
        ttk.Button(log_controls, text="Очистить логи", command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(log_controls, text="Сохранить логи в файл", command=self.save_logs).pack(side=tk.LEFT, padx=5)
        
        log_frame = ttk.Frame(self.log_tab)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, bg='#1e1e1e', fg='white', font=('Consolas', 10), height=20)
        
        self.log_text.tag_config('red', foreground='#ff6b6b')
        self.log_text.tag_config('orange', foreground='#ffa94d')
        self.log_text.tag_config('green', foreground='#69db7c')
        self.log_text.tag_config('blue', foreground='#74c0fc')
        self.log_text.tag_config('white', foreground='#ffffff')
        
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.log_info("Интерфейс логов успешно проинициализирован.")
    
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
        self.log_info("Окно логов очищено.")
    
    def save_logs(self):
        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")]
        )
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.get(1.0, tk.END))
                self.log_success(f"Логи сохранены в файл: {file_path}")
            except Exception as e:
                self.log_error(f"Не удалось выгрузить логи: {e}")
    
    def create_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Копировать IP адрес", command=self.copy_ip)
        self.context_menu.add_command(label="Копировать серийный номер", command=self.copy_serial)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Обновить список устройств", command=self.scan_cameras)
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
                if ip and ip not in ['N/A', 'Unknown']:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(ip)
                    self.status_bar.config(text=f"Скопировано в буфер обмена IP: {ip}")
    
    def copy_serial(self):
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], 'values')
            if values and len(values) >= 3:
                serial = values[2]
                if serial and serial not in ['N/A', 'Unknown']:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(serial)
                    self.status_bar.config(text=f"Скопировано в буфер обмена SN: {serial}")
    
    def scan_cameras(self):
        self.log_info("Сканирование локальной сети на наличие камер Lucid Triton GigE...")
        self.scan_btn.config(state=tk.DISABLED)
        self.loading_label.config(text="Поиск GigE устройств...")
        self.status_bar.config(text="Поиск сетевых камер в процессе...")
        Thread(target=self._scan_cameras_thread, daemon=True).start()
    
    def _scan_cameras_thread(self):
        try:
            self.cameras = CameraScanner.scan_all()
            self.root.after(0, self._update_camera_list)
            self.log_success(f"Поиск завершен. Найдено камер Lucid GigE в подсети: {len(self.cameras)}")
        except Exception as e:
            error_msg = f"Критическая ошибка сканирования шины: {e}"
            self.log_error(error_msg)
            self.root.after(0, lambda: self._show_error(error_msg))
    
    def _update_camera_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        if not self.cameras:
            self.tree.insert('', 'end', values=('Устройства Lucid Triton не обнаружены', '—', '—', '—', '—'))
            self.status_bar.config(text="Устройства не найдены. Проверьте питание и Ethernet-кабель.")
        else:
            for cam in self.cameras:
                status = cam.get('status', 'Unknown')
                status_display = {'Available': 'Доступна', 'Connected': 'Подключена', 'Failed': 'Ошибка интерфейса'}.get(status, 'Неизвестно')
                ip = cam.get('ip', 'N/A') or 'N/A'
                
                self.tree.insert(
                    '', 'end',
                    values=(cam.get('name', 'Unknown Triton'), 'GigE Vision', cam.get('serial', 'N/A'), ip, status_display),
                    tags=('lucid',)
                )
            self.status_bar.config(text=f"Обнаружено активных сетевых GigE камер: {len(self.cameras)}")
        
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
                self.status_bar.config(text=f"Выбран стек: {self.selected_camera.get('name')} | Заводской номер SN: {self.selected_camera.get('serial')}")
    
    def connect_camera(self):
        if not self.selected_camera:
            messagebox.showwarning("Предупреждение", "Пожалуйста, сначала выберите физическое устройство из списка.")
            return
    
        self.stop_video_stream()
        if self.is_recording:
            self.stop_recording()
        
        camera_name = self.selected_camera.get('name', 'Unknown')
        device_id = self.selected_camera.get('device_id', 0)
        saved_ip = self.selected_camera.get('_saved_ip', None)
        
        try:
            self.log_info(f"Инициализация сокета и подключение к {camera_name}...")
            self.connect_btn.config(state=tk.DISABLED)
            
            # Передача аргумента "saved_ip" в фабрику исправлена для соответствия бэкенду
            self.current_camera = create_camera('lucid', device_id=device_id, saved_ip=saved_ip, pixel_format="Mono8")
            
            self._update_camera_status(camera_name, 'Connected')
            self.log_success(f"Прямой стрим-канал с устройством {camera_name} установлен.")
            
            self.show_video_btn.config(state=tk.NORMAL)
            self.video_control_btn.config(state=tk.NORMAL)
            self.snapshot_btn.config(state=tk.NORMAL)
            self.record_btn.config(state=tk.NORMAL)
            self.video_info_label.config(text=f"Камера: {camera_name} (GigE Vision)")
            
        except Exception as e:
            error_msg = f"Ошибка дескриптора Arena API при связи с камерой: {e}"
            self.log_error(error_msg)
            messagebox.showerror("Ошибка подключения", error_msg)
            self.current_camera = None
        finally:
            self.connect_btn.config(state=tk.NORMAL)
    
    def _update_camera_status(self, camera_name, new_status):
        for item in self.tree.get_children():
            values = self.tree.item(item, 'values')
            if values and values[0] == camera_name:
                new_values = list(values)
                new_values[4] = 'Подключена' if new_status == 'Connected' else 'Доступна'
                self.tree.item(item, values=tuple(new_values))
                break
    
    def refresh_status(self):
        self.scan_cameras()
    
    def show_video(self):
        self.notebook.select(self.video_tab)
        if not self.is_streaming:
            self.toggle_video()
    
    def toggle_video(self):
        if self.is_streaming:
            self.stop_video_stream()
        else:
            self.start_video_stream()

    def start_video_stream(self):
        if not self.current_camera:
            return
        if self.is_streaming:
            return
        
        self.is_streaming = True
        self.video_control_btn.config(text="Остановить поток")
        self.video_status.config(text="Статус: Активация матрицы, прием буферов...")
        
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
        self.video_control_btn.config(text="Запустить поток")
        if self.canvas_image_id:
            self.video_canvas.delete(self.canvas_image_id)
            self.canvas_image_id = None
        self.video_status.config(text="Статус: Захват буферов приостановлен")
    
    def _capture_loop(self):
        while self.is_streaming and self.current_camera:
            try:
                frame = self.current_camera.get_frame()
                if frame is not None:
                    display_frame = frame.copy()
                    height, width = display_frame.shape[:2]
                    font_scale = width / 800
                    font_thickness = max(1, int(font_scale * 2))
                    
                    # Рендеринг экранного OSD-таймера при записи
                    if self.is_recording and self.recording_start_time:
                        elapsed = time.time() - self.recording_start_time
                        minutes = int((elapsed % 3600) // 60)
                        seconds = int(elapsed % 60)
                        time_str = f"REC {minutes:02d}:{seconds:02d}"
                        time_pos = (width - int(160 * font_scale), int(40 * font_scale))
                        cv2.putText(display_frame, time_str, time_pos, cv2.FONT_HERSHEY_SIMPLEX, font_scale * 0.7, (0, 0, 255), font_thickness, cv2.LINE_AA)
                    
                    # Вывод текущего аппаратного FPS на экран
                    fps_text = f"FPS: {self.current_fps:.1f}"
                    fps_pos = (width - int(120 * font_scale), height - int(30 * font_scale))
                    cv2.putText(display_frame, fps_text, fps_pos, cv2.FONT_HERSHEY_SIMPLEX, font_scale * 0.5, (0, 255, 255), font_thickness - 1, cv2.LINE_AA)
                    
                    # Расчет частоты кадров
                    self.frame_count += 1
                    current_time = time.time()
                    elapsed = current_time - self.fps_start_time
                    
                    if elapsed >= self.fps_update_interval:
                        self.current_fps = self.frame_count / elapsed
                        self.frame_count = 0
                        self.fps_start_time = current_time
                        self.root.after(0, self._update_fps_display)
                    
                    # Сброс сырого чистого кадра (без графики OSD) в дисковый видеорегистратор
                    if self.is_recording and self.video_recorder:
                        self.video_recorder.write_frame(frame)
                    
                    # Конвертация цветовой палитры под Tkinter канвас
                    frame_rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
                    max_width = self.video_frame.winfo_width() or 1024
                    max_height = self.video_frame.winfo_height() or 768
                    
                    # Динамический ресайз под текущее соотношение сторон окна
                    if width > max_width or height > max_height:
                        scale = min(max_width/width, max_height/height)
                        new_width = int(width * scale)
                        new_height = int(height * scale)
                        if new_width > 0 and new_height > 0:
                            frame_rgb = cv2.resize(frame_rgb, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
                    
                    img = Image.fromarray(frame_rgb)
                    imgtk = ImageTk.PhotoImage(image=img)
                    
                    with self.frame_lock:
                        self.last_frame = imgtk
                        self.frame_ready = True
                else:
                    time.sleep(0.001)
            except Exception as e:
                break
    
    def update_ui_loop(self):
        if self.is_streaming and self.frame_ready:
            with self.frame_lock:
                if self.last_frame is not None:
                    if self.canvas_image_id:
                        self.video_canvas.delete(self.canvas_image_id)
                    
                    x = (self.video_canvas.winfo_width() - self.last_frame.width()) // 2
                    y = (self.video_canvas.winfo_height() - self.last_frame.height()) // 2
                    self.canvas_image_id = self.video_canvas.create_image(max(0, x), max(0, y), anchor=tk.NW, image=self.last_frame)
                    self.video_canvas.image = self.last_frame
                    self.frame_ready = False
                    self.video_status.config(text="Статус: Стрим активен")
        self.root.after(1, self.update_ui_loop)
    
    def _update_fps_display(self):
        self.fps_label.config(text=f"FPS: {self.current_fps:.1f}")
    
    def toggle_recording(self):
        if not self.is_streaming:
            messagebox.showwarning("Предупреждение", "Невозможно начать запись: запустите поток видео.")
            return
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()
    
    def start_recording(self):
        if not self.current_camera or not self.is_streaming:
            return
        
        try:
            recordings_dir = filedialog.askdirectory(title="Укажите директорию для сохранения видеофайла")
            if not recordings_dir:
                return
                
            camera_info = self.current_camera.get_info()
            camera_name = camera_info.get('name', 'Lucid_Triton')
            
            frame = None
            for _ in range(5):
                frame = self.current_camera.get_frame()
                if frame is not None:
                    break
                time.sleep(0.1)
            
            if frame is None:
                self.log_error("Не удалось прочитать проверочный кадр сенсора матрицы.")
                return
                
            height, width = frame.shape[:2]
            
            mode_map = {
                'Сжатый (XVID)': 'lossy',
                'Без потерь (HFYU)': 'lossless',
                'Высокое качество (MJPG)': 'high_quality',
                'Кадры PNG (без потерь)': 'png_frames'
            }
            record_mode = mode_map.get(self.record_mode_var.get(), 'lossy')
            
            # # ЗАЩИТА ОТ КРАША: Если поток только запущен и FPS равен 0, 
            # # выставляем стабильный базовый FPS для промышленной GigE камеры (25 кадров/сек)
            # safe_fps = int(self.current_fps)
            # if safe_fps < 5:
            #     safe_fps = 25  
                
            self.video_recorder = VideoRecorder(
                output_dir=recordings_dir,
                fps=self.current_fps,
                mode=record_mode
            )
            
            self.current_record_path = self.video_recorder.start_recording(width, height, camera_name)
            self.recording_start_time = time.time()
            self.is_recording = True
            self.record_btn.config(text="Остановить запись")
            self.recording_label.config(text="Запись: ИДЕТ", foreground='#ff0000')
            self.log_success(f"Запущено сохранение сессии захвата: {self.current_record_path}")
            
        except Exception as e:
            self.log_error(f"Не удалось инициализировать рекордер файлов: {e}")
    
    def stop_recording(self):
        if not self.is_recording or not self.video_recorder:
            return
        try:
            saved_path = self.video_recorder.stop_recording()
            self.is_recording = False
            self.recording_start_time = None
            self.video_recorder = None
            self.record_btn.config(text="Записать видео")
            self.recording_label.config(text="Запись: Нет", foreground="#3010c2")
            
            if saved_path:
                self.log_success(f"Video-файл успешно сохранен: {saved_path}")
                if messagebox.askyesno("Запись сохранена", f"Файл успешно сформирован:\n{saved_path}\n\nОткрыть целевую директорию?"):
                    os.startfile(os.path.dirname(saved_path))
        except Exception as e:
            self.log_error(f"Ошибка при закрытии медиаконтейнера: {e}")
            self.is_recording = False
    
    def take_snapshot(self):
        if not self.is_streaming or not self.current_camera:
            return
        try:
            frame = self.current_camera.get_frame()
            if frame is not None:
                snapshots_dir = filedialog.askdirectory(title="Выберите каталог для сохранения снимка")
                if not snapshots_dir:
                    return
                    
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                camera_name = self.current_camera.get_info().get('name', 'Lucid_Camera')
                safe_name = re.sub(r'[^\w\-_\. ]', '_', camera_name)
                filepath = os.path.join(snapshots_dir, f"{safe_name}_{timestamp}.png")
                
                cv2.imwrite(filepath, frame)
                self.log_success(f"Статический PNG-снимок матрицы сохранен: {filepath}")
        except Exception as e:
            self.log_error(f"Не удалось записать файл снимка: {e}")
            
        def _show_error(self, error_message):
            self.scan_btn.config(state=tk.NORMAL)
            self.loading_label.config(text="")
            self.status_bar.config(text=f"Сбой шины данных: {error_message}")
            messagebox.showerror("Системная ошибка", error_message)

def main():
    root = tk.Tk()
    app = CameraDiscoveryApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()