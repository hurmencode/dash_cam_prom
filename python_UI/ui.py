import faulthandler
import logging
import os
import platform
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime
from threading import Lock, Thread
from tkinter import PhotoImage, filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT_DIR)

from backend import camera_manager as cm  # noqa: E402
from backend.camera_manager import CameraScanner, create_camera  # noqa: E402
from backend.config import LOG_DIR, load_settings  # noqa: E402
from backend.recorder import (  # noqa: E402
    SegmentedRecorder, ffmpeg_selftest, free_mb, set_ffmpeg_path)

log = logging.getLogger("dashcam.ui")


# ======================================================================
#  Логирование: один механизм для UI, файла и консоли, потокобезопасный
# ======================================================================
class QueueLogHandler(logging.Handler):
    """Складывает записи в очередь; Tk-виджет читает её из главного потока."""

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put_nowait((record.levelno, self.format(record)))
        except Exception:
            pass


def setup_logging(log_queue):
    """Возвращает (file_handler, путь к файлу)."""
    root = logging.getLogger("dashcam")
    root.setLevel(logging.INFO)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    file_handler, path = None, None
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(
            LOG_DIR, f"session_{datetime.now():%Y%m%d_%H%M%S}.log")
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        # Python-трейсбек при segfault в C-библиотеках (OpenCV, Aravis)
        faulthandler.enable(file=file_handler.stream, all_threads=True)
    except Exception as e:
        print(f"Не удалось открыть файл логов: {e}", file=sys.stderr)

    qh = QueueLogHandler(log_queue)
    qh.setFormatter(fmt)
    root.addHandler(qh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return file_handler, path


def _delete_old_logs(keep: int = 30):
    try:
        files = sorted(f for f in os.listdir(LOG_DIR)
                       if f.startswith("session_") and f.endswith(".log"))
        for f in files[:-keep]:
            os.remove(os.path.join(LOG_DIR, f))
    except Exception:
        pass


# ======================================================================
class SlotState:
    """Состояние одного слота камеры."""

    def __init__(self, name: str):
        self.name = name
        self.camera = None
        self.camera_info = None

        # поток захвата
        self.thread = None
        self.stop_evt = threading.Event()
        self.current_fps = 0.0
        self.last_frame_time = 0.0
        self.stalled = False
        self.frame_size = None          # (w, h)
        self.frames_total = 0

        # последний кадр (сырой) и кадр для превью
        self.frame_lock = Lock()
        self.last_frame = None
        self.last_frame_ts = 0.0
        self.preview_img = None
        self.preview_seq = 0
        self.rendered_seq = 0
        self.last_preview_decode = 0.0

        # автоэкспозиция (состояние меняет только поток захвата)
        self.ae_request = False         # поставлен хуком начала сегмента
        self.ae_lock_at = None          # когда зафиксировать экспозицию
        self.ae_unsupported = False

        # запись
        self.rec_lock = Lock()
        self.arm_pending = False
        self.record_dir = None
        self.recorder = None

        # окно превью
        self.preview_window = None
        self.preview_canvas = None
        self.photo = None
        self.photo_key = None
        self.last_render = 0.0

        # виджеты
        self.slot_info_label = None
        self.fps_label = None
        self.rec_label = None
        self.show_btn = None


class CameraDiscoveryApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Видеорегистратор 3000")
        self.root.geometry("1100x700")
        self._closing = False
        self._busy = False
        self._stopping = False
        self._tick = 0
        self._disk_dialog_shown = False
        self._widget_cache = {}

        self.log_queue = queue.Queue()
        self._events = queue.Queue()      # вызовы для главного потока
        self.log_file_handler, self.log_file_path = setup_logging(
            self.log_queue)
        _delete_old_logs()

        self.settings = load_settings()
        set_ffmpeg_path(self.settings.ffmpeg_path)

        icon_path = os.path.join(ROOT_DIR, "favicon.png")
        if os.path.exists(icon_path):
            try:
                self.root.iconphoto(False, PhotoImage(file=icon_path))
            except Exception as e:
                log.debug("иконка: %s", e)

        self.slots = {"slot_1": SlotState("slot_1"),
                      "slot_2": SlotState("slot_2")}
        self.cameras = []
        self.connected_cameras = {}

        self.create_widgets()
        self.install_crash_handlers()
        self.install_hotkeys()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(500, lambda: self.scan_cameras(user=False))
        self.root.after(30, self.update_ui_loop)

        log.info("Сессия логов: %s", self.log_file_path)
        if cm.Aravis is None:
            log.warning("Aravis не найден — GigE-камеры недоступны")
        else:
            log.info("Aravis %s", cm.ARAVIS_VERSION)
        # Проверка ffmpeg в фоне: проблему видно в логе сразу, а не в
        # момент начала записи.
        Thread(target=lambda: [ffmpeg_selftest("copy_mjpeg", True),
                               ffmpeg_selftest("mjpeg", False)],
               daemon=True, name="ffmpeg-check").start()
        log.info("Кодек: %s, контейнер: %s, сегмент: %d с, мин. место: "
                 "%d MB", self.settings.encoder, self.settings.container,
                 self.settings.segment_seconds, self.settings.min_free_mb)

    # ------------------------------------------------------------------
    #  Вспомогательное
    # ------------------------------------------------------------------
    def _post(self, fn):
        """Выполнить fn в главном потоке (можно вызывать из любого)."""
        self._events.put(fn)

    def _set(self, widget, **kw):
        """config() только при изменении — меньше перерисовок."""
        key = (id(widget), tuple(sorted(kw.items())))
        if self._widget_cache.get(id(widget)) == key:
            return
        self._widget_cache[id(widget)] = key
        widget.config(**kw)

    def _slot_index(self, slot):
        return list(self.slots.values()).index(slot) + 1

    def _rec_active(self):
        return any(s.recorder is not None or s.arm_pending
                   for s in self.slots.values())

    def _connected_slots(self):
        return [s for s in self.slots.values() if s.camera is not None]

    # ------------------------------------------------------------------
    #  Аварийные обработчики
    # ------------------------------------------------------------------
    def install_crash_handlers(self):
        original_hook = sys.excepthook

        def excepthook(exc_type, exc_value, exc_tb):
            log.critical("НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ",
                         exc_info=(exc_type, exc_value, exc_tb))
            original_hook(exc_type, exc_value, exc_tb)

        sys.excepthook = excepthook

        def thread_hook(args):
            log.critical("ИСКЛЮЧЕНИЕ В ПОТОКЕ %s", args.thread.name,
                         exc_info=(args.exc_type, args.exc_value,
                                   args.exc_traceback))

        threading.excepthook = thread_hook

        # Исключения в Tk-колбэках иначе уходят только в stderr
        self.root.report_callback_exception = (
            lambda et, ev, tb: log.error(
                "Исключение в обработчике UI", exc_info=(et, ev, tb)))

        def signal_handler(signum, frame):
            log.warning("Получен сигнал %s, завершаю работу...", signum)
            try:
                self.root.after(0, self.on_closing)
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
        except Exception as e:
            log.warning("Обработчики сигналов не установлены: %s", e)

    # ------------------------------------------------------------------
    #  Горячие клавиши
    # ------------------------------------------------------------------
    def install_hotkeys(self):
        self.root.bind_all("<Key>", self._on_key)

    def _on_key(self, event):
        if isinstance(self.root.focus_get(), (tk.Text, tk.Entry,
                                              ttk.Entry)):
            return
        k = event.keysym
        if k in ("r", "R", "Cyrillic_ka", "Cyrillic_KA"):
            self.toggle_recording()
        elif k in ("s", "S", "Cyrillic_yeru", "Cyrillic_YERU"):
            self.take_snapshot()
        elif k == "space":
            self.toggle_all_previews()
        elif k in ("l", "L", "Cyrillic_el", "Cyrillic_EL"):
            self.notebook.select(self.log_tab)

    # ------------------------------------------------------------------
    #  Закрытие
    # ------------------------------------------------------------------
    def on_closing(self):
        if self._closing:
            return
        self._closing = True
        log.info("Завершение работы...")

        # 1. запись: дописать очередь и закрыть файлы (без диалогов)
        for slot in self.slots.values():
            with slot.rec_lock:
                slot.arm_pending = False
                rec, slot.recorder = slot.recorder, None
            if rec is not None:
                try:
                    paths = rec.stop()
                    log.info("[%s] записано кадров: %d, потеряно: %d, "
                             "файлов: %d", slot.name, rec.frames_written,
                             rec.frames_dropped, len(paths))
                except Exception:
                    log.exception("остановка записи")

        # 2. камеры
        for slot in self.slots.values():
            self._close_preview_window(slot, silent=True)
            self._release_slot(slot)

        self.settings.save()
        log.info("Приложение завершило работу, логи сохранены.")
        try:
            self.root.destroy()
        except Exception:
            pass
        logging.shutdown()

    # ------------------------------------------------------------------
    #  Виджеты
    # ------------------------------------------------------------------
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
        top = ttk.Frame(self.camera_tab, padding="10")
        top.pack(fill=tk.X)

        self.scan_btn = ttk.Button(
            top, text="Поиск камер", width=20,
            command=lambda: self.scan_cameras(user=True))
        self.scan_btn.pack(side=tk.LEFT, padx=5)

        self.connect_btn = ttk.Button(
            top, text="Подключиться", width=20,
            command=self.connect_camera, state=tk.DISABLED)
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        self.disconnect_btn = ttk.Button(
            top, text="Отключить все", width=20,
            command=lambda: self.disconnect_all(user=True))
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)

        self.loading_label = ttk.Label(top, text="")
        self.loading_label.pack(side=tk.LEFT, padx=20)

        ttk.Label(
            self.camera_tab, foreground="#666666", justify=tk.LEFT,
            text="Выберите одну или две камеры (Ctrl+Click). "
                 "Максимум 2 камеры одновременно.\n"
                 "Для просмотра видео и записи перейдите на вкладку "
                 "«Управление»."
        ).pack(anchor=tk.W, padx=10, pady=(0, 5))

        columns = ("name", "type", "serial", "ip", "status")
        self.status_bar = ttk.Label(
            self.camera_tab, relief=tk.SUNKEN,
            text="Готов к работе. Нажмите 'Поиск камер'")
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        self.tree = ttk.Treeview(self.camera_tab, columns=columns,
                                 show="headings", height=15,
                                 selectmode="extended")
        for col, text in zip(columns, ["Имя камеры", "Тип",
                                       "Серийный номер", "IP-адрес",
                                       "Статус"]):
            self.tree.heading(col, text=text)
            self.tree.column(col, anchor="center")

        sb = ttk.Scrollbar(self.camera_tab, orient=tk.VERTICAL,
                           command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                       padx=(10, 0), pady=10)
        sb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=10)
        self.tree.bind("<<TreeviewSelect>>", self.on_select_camera)

    def create_video_tab(self):
        controls = ttk.Frame(self.video_tab, padding="10")
        controls.pack(fill=tk.X)

        self.recording_label = ttk.Label(
            controls, text="Запись: Нет",
            font=("Arial", 11, "bold"), foreground="#3010c2")
        self.recording_label.pack(side=tk.LEFT, padx=5)

        self.disk_label = ttk.Label(
            controls, text="Диск: —",
            font=("Arial", 11, "bold"), foreground="#555555")
        self.disk_label.pack(side=tk.LEFT, padx=20)

        self.record_btn = ttk.Button(
            controls, text="Записать видео", width=18,
            command=self.toggle_recording, state=tk.DISABLED)
        self.record_btn.pack(side=tk.RIGHT, padx=5)

        self.snapshot_btn = ttk.Button(
            controls, text="Снимок", width=15,
            command=self.take_snapshot, state=tk.DISABLED)
        self.snapshot_btn.pack(side=tk.RIGHT, padx=5)

        # Папка записи
        dir_row = ttk.Frame(self.video_tab, padding=(10, 0))
        dir_row.pack(fill=tk.X)
        ttk.Label(dir_row, text="Папка записи:").pack(side=tk.LEFT, padx=5)
        self.dir_label = ttk.Label(
            dir_row, foreground="#333333",
            text=self.settings.last_record_dir or "не выбрана")
        self.dir_label.pack(side=tk.LEFT, padx=5)
        self.dir_btn = ttk.Button(dir_row, text="Выбрать…", width=12,
                                  command=self.choose_record_dir)
        self.dir_btn.pack(side=tk.LEFT, padx=5)

        # Превью
        preview = ttk.Frame(self.video_tab, padding="10")
        preview.pack(fill=tk.X)
        self.preview_all_btn = ttk.Button(
            preview, text="Показать все", width=20,
            command=self.toggle_all_previews, state=tk.DISABLED)
        self.preview_all_btn.pack(side=tk.LEFT, padx=5)
        ttk.Label(
            preview, foreground="#666666",
            text="Горячие клавиши: R — запись, S — снимок, "
                 "Пробел — превью, L — логи"
        ).pack(side=tk.LEFT, padx=10)

        slots_frame = ttk.LabelFrame(
            self.video_tab, text="Подключённые камеры", padding="10")
        slots_frame.pack(fill=tk.X, padx=10, pady=10)

        for i, (slot_name, slot) in enumerate(self.slots.items()):
            row = ttk.Frame(slots_frame)
            row.pack(fill=tk.X, pady=3)

            slot.slot_info_label = ttk.Label(
                row, text=f"Слот {i + 1}: —", width=40, anchor="w",
                font=("Arial", 10, "bold"))
            slot.slot_info_label.pack(side=tk.LEFT, padx=5)

            slot.fps_label = ttk.Label(row, text="FPS: —", width=22,
                                       anchor="w", font=("Arial", 10))
            slot.fps_label.pack(side=tk.LEFT, padx=5)

            slot.rec_label = ttk.Label(row, text="", anchor="w",
                                       font=("Arial", 10),
                                       foreground="#555555")
            slot.rec_label.pack(side=tk.LEFT, padx=5, fill=tk.X,
                                expand=True)

            slot.show_btn = ttk.Button(
                row, text="Показать", width=12, state=tk.DISABLED,
                command=lambda sn=slot_name: self.toggle_preview(sn))
            slot.show_btn.pack(side=tk.RIGHT, padx=5)

        self.video_status = ttk.Label(
            self.video_tab, text="Статус: Ожидание",
            relief=tk.SUNKEN, anchor=tk.W)
        self.video_status.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

    def create_log_tab(self):
        ctl = ttk.Frame(self.log_tab, padding="5")
        ctl.pack(fill=tk.X)
        ttk.Button(ctl, text="Очистить логи",
                   command=self.clear_logs).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctl, text="Сохранить логи",
                   command=self.save_logs).pack(side=tk.LEFT, padx=5)

        frame = ttk.Frame(self.log_tab)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text = tk.Text(frame, wrap=tk.WORD, bg="#1e1e1e",
                                fg="white", font=("Consolas", 10),
                                height=20)
        self.log_text.tag_config("red", foreground="#ff6b6b")
        self.log_text.tag_config("orange", foreground="#ffa94d")
        self.log_text.tag_config("blue", foreground="#74c0fc")
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                           command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ------------------------------------------------------------------
    #  Логи в UI
    # ------------------------------------------------------------------
    def _drain_log_queue(self):
        n = 0
        try:
            while n < 200:
                level, line = self.log_queue.get_nowait()
                tag = ("red" if level >= logging.ERROR else
                       "orange" if level >= logging.WARNING else "blue")
                self.log_text.insert(tk.END, line + "\n", (tag,))
                n += 1
        except queue.Empty:
            pass
        if n:
            # не даём окну логов расти бесконечно (сутки записи)
            if int(float(self.log_text.index("end-1c").split(".")[0])) \
                    > 5000:
                self.log_text.delete("1.0", "1000.0")
            self.log_text.see(tk.END)

    def clear_logs(self):
        self.log_text.delete("1.0", tk.END)
        log.info("Окно логов очищено (файл сессии сохранён полностью)")

    def save_logs(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                       ("All files", "*.*")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.log_text.get("1.0", tk.END))
                log.info("Логи сохранены в %s", path)
            except Exception as e:
                log.error("Не удалось сохранить логи: %s", e)

    # ------------------------------------------------------------------
    #  Список камер
    # ------------------------------------------------------------------
    @staticmethod
    def get_type_display(camera_type: str) -> str:
        return {"aravis": "GigE", "gige": "GigE", "usbcam": "USB",
                "unknown": "Unknown"}.get(camera_type,
                                           str(camera_type).upper())

    @staticmethod
    def _camera_key(cam):
        if cam.get("type") == "usbcam":
            return ("usb", cam.get("device_id", 0))
        serial = cam.get("serial")
        if serial and serial not in ("", "N/A", "Unknown"):
            return ("gige", serial)
        return ("gige", cam.get("ip", "N/A"))

    def _set_busy(self, busy, text=""):
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.scan_btn.config(state=state)
        self.loading_label.config(text=text)
        if busy:
            self.connect_btn.config(state=tk.DISABLED)

    def scan_cameras(self, user=True):
        if self._busy or self._closing:
            return
        if self._rec_active() or self._stopping:
            messagebox.showwarning(
                "Идёт запись",
                "Поиск камер отключает все камеры. "
                "Сначала остановите запись.")
            return
        if user and self._connected_slots():
            if not messagebox.askyesno(
                    "Поиск камер",
                    "Поиск камер отключит подключённые камеры. "
                    "Продолжить?"):
                return
        self.disconnect_all(silent=True)

        log.info("Запуск сканирования камер...")
        self._set_busy(True, "Поиск камер...")
        self.status_bar.config(text="Выполняется сканирование камер...")
        Thread(target=self._scan_thread, daemon=True,
               name="scan").start()

    def _scan_thread(self):
        try:
            cams = CameraScanner.scan_all()
            self._post(lambda: self._update_camera_list(cams))
        except Exception as e:
            log.exception("Ошибка сканирования")
            msg = f"Ошибка сканирования: {e}"
            self._post(lambda: self._scan_failed(msg))

    def _scan_failed(self, msg):
        self._set_busy(False)
        self.status_bar.config(text=msg)
        messagebox.showerror("Ошибка", msg)

    def _update_camera_list(self, cams):
        self.cameras = cams
        self.tree.delete(*self.tree.get_children())

        if not cams:
            self.tree.insert("", "end", iid="none",
                             values=("Нет камер", "—", "—", "—", "—"))
            self.status_bar.config(text="Камеры не найдены")
            log.warning("Камеры не найдены")
        else:
            for i, cam in enumerate(cams):
                self.tree.insert("", "end", iid=str(i), values=(
                    cam.get("name", "Unknown"),
                    self.get_type_display(cam.get("type")),
                    cam.get("serial", "N/A"),
                    cam.get("ip") or "N/A", "Доступна"))
            usb = sum(1 for c in cams if c.get("type") == "usbcam")
            gige = len(cams) - usb
            parts = ([f"GigE: {gige}"] if gige else []) + \
                    ([f"USB: {usb}"] if usb else [])
            self.status_bar.config(
                text=f"Найдено камер: {len(cams)} ({', '.join(parts)})")
        self._set_busy(False)
        self.connect_btn.config(state=tk.DISABLED)
        log.info("Сканирование завершено. Найдено камер: %d", len(cams))

    def _refresh_table_statuses(self):
        for iid in self.tree.get_children():
            if not iid.isdigit() or int(iid) >= len(self.cameras):
                continue
            key = self._camera_key(self.cameras[int(iid)])
            vals = list(self.tree.item(iid, "values"))
            vals[4] = ("Подключена" if key in self.connected_cameras
                       else "Доступна")
            self.tree.item(iid, values=tuple(vals))

    def on_select_camera(self, event):
        sel = [i for i in self.tree.selection() if i.isdigit()]
        self.connect_btn.config(
            state=tk.NORMAL if sel and not self._busy else tk.DISABLED)
        if sel:
            names = [self.tree.item(i, "values")[0] for i in sel]
            self.status_bar.config(text=f"Выбрано: {', '.join(names)}")

    # ------------------------------------------------------------------
    #  Подключение
    # ------------------------------------------------------------------
    def connect_camera(self):
        if self._busy:
            return
        sel = [int(i) for i in self.tree.selection() if i.isdigit()]
        cams = [self.cameras[i] for i in sel if i < len(self.cameras)]
        if not cams:
            messagebox.showwarning("Предупреждение",
                                   "Сначала выберите камеру")
            return

        todo = []
        for cam in cams:
            if self._camera_key(cam) in self.connected_cameras:
                log.warning("%s уже подключена — пропускаю",
                            cam.get("name"))
            else:
                todo.append(cam)
        if not todo:
            messagebox.showinfo("Уже подключено",
                                "Все выбранные камеры уже подключены.")
            return

        free = [n for n, s in self.slots.items() if s.camera is None]
        if len(todo) > len(free):
            messagebox.showwarning(
                "Слишком много камер",
                f"Свободных слотов: {len(free)}. "
                f"Максимум 2 камеры одновременно.")
            todo = todo[:len(free)]
        pairs = list(zip(todo, free))

        self._set_busy(True, "Подключение...")
        Thread(target=self._connect_worker, args=(pairs,),
               daemon=True, name="connect").start()

    def _connect_worker(self, pairs):
        results = []
        for cam, slot_name in pairs:
            try:
                log.info("Подключение к %s (%s)...", cam["name"],
                         self.get_type_display(cam.get("type")))
                if cam.get("type") == "usbcam":
                    new = create_camera("usbcam", self.settings,
                                        device_id=cam.get("device_id", 0))
                else:
                    new = create_camera("aravis", self.settings,
                                        arv_id=cam.get("arv_id"),
                                        pixel_format="Mono8")
                results.append((cam, slot_name, new, None))
            except Exception as e:
                log.error("Не удалось подключить %s: %s", cam["name"], e)
                results.append((cam, slot_name, None, e))
        self._post(lambda: self._on_connected(results))

    def _on_connected(self, results):
        ok, failed = [], []
        for cam, slot_name, new_cam, err in results:
            if new_cam is None:
                failed.append(cam["name"])
                continue
            if self._closing:
                new_cam.release()
                continue
            slot = self.slots[slot_name]
            slot.camera = new_cam
            slot.camera_info = cam
            self.connected_cameras[self._camera_key(cam)] = slot_name

            kind = {"mjpeg_raw": "MJPG без перекодирования",
                    "gray": "Mono8", "bgr": "BGR"}.get(
                        new_cam.frame_kind, "")
            slot.slot_info_label.config(
                text=f"Слот {self._slot_index(slot)}: "
                     f"{self.get_type_display(cam.get('type'))} — "
                     f"{cam.get('name')}")
            slot.show_btn.config(state=tk.NORMAL)
            self._start_capture(slot)
            log.info("Подключено: %s → %s (%s)", cam.get("name"),
                     slot_name, kind)
            ok.append(cam.get("name"))

        self._set_busy(False)
        self._refresh_table_statuses()
        self._refresh_controls()

        if ok:
            msg = "Успешно подключено:\n  • " + "\n  • ".join(ok)
            if failed:
                msg += ("\n\nНе удалось подключить:\n  • "
                        + "\n  • ".join(failed))
            messagebox.showinfo("Подключение", msg)
        elif failed:
            messagebox.showerror(
                "Ошибка подключения",
                "Не удалось подключить:\n  • " + "\n  • ".join(failed)
                + "\n\nПодробности — на вкладке «Логи».")

    def _release_slot(self, slot):
        """Останавливает захват и освобождает камеру (безопасный порядок)."""
        slot.stop_evt.set()
        t = slot.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
            if t.is_alive():
                # release() под работающим get_frame() — это segfault.
                # Лучше оставить камеру открытой, чем уронить процесс.
                log.error("[%s] поток захвата не остановился — камера "
                          "не освобождается", slot.name)
                return
        slot.thread = None
        cam, slot.camera = slot.camera, None
        if cam is not None:
            try:
                if slot.ae_lock_at is not None:     # не оставляем камеру в авто
                    slot.ae_lock_at = None
                    cam.set_auto_exposure(False,
                                          self.settings.auto_exposure_gain)
                stats = cam.get_stats()
                if stats:
                    log.info("[%s] статистика потока: %s", slot.name, stats)
                cam.release()
            except Exception as e:
                log.error("Ошибка освобождения %s: %s", slot.name, e)
        slot.camera_info = None
        slot.last_frame = None
        slot.preview_img = None
        slot.current_fps = 0.0

    def disconnect_all(self, silent=False, user=False):
        if user and (self._rec_active() or self._stopping):
            if not messagebox.askyesno(
                    "Идёт запись",
                    "Остановить запись и отключить камеры?"):
                return
        self._stop_all_recorders(sync=True)
        for slot in self.slots.values():
            self._close_preview_window(slot, silent=True)
            self._release_slot(slot)
            idx = self._slot_index(slot)
            slot.slot_info_label.config(text=f"Слот {idx}: —")
            slot.fps_label.config(text="FPS: —")
            slot.rec_label.config(text="")
            slot.show_btn.config(state=tk.DISABLED, text="Показать")
        self.connected_cameras.clear()
        self._refresh_controls()
        if not silent:
            log.info("Все камеры отключены")
            self._refresh_table_statuses()

    # ------------------------------------------------------------------
    #  Поток захвата
    # ------------------------------------------------------------------
    def _start_capture(self, slot):
        slot.stop_evt = threading.Event()
        slot.frame_size = None
        slot.current_fps = 0.0
        slot.frames_total = 0
        slot.stalled = False
        slot.last_frame_time = time.time()
        slot.ae_request, slot.ae_lock_at = False, None
        slot.ae_unsupported = False
        slot.thread = Thread(
            target=self._capture_loop,
            args=(slot, slot.camera, slot.stop_evt),
            name=f"capture-{slot.name}", daemon=True)
        slot.thread.start()

    def _capture_loop(self, slot, cam, stop):
        """
        Единственный потребитель кадров камеры. Здесь же:
        подсчёт FPS, подготовка превью, передача кадра рекордеру.
        Tk-виджетов не трогает.
        """
        s = self.settings
        fps_t0, fps_n = time.time(), 0
        errors = 0
        warm, warm_n, warm_t0 = [], 0, None
        max_warm = max(1, s.queue_frames - 8)
        raw = cam.frame_kind == "mjpeg_raw"

        while not stop.is_set():
            try:
                frame = cam.get_frame(200)
                errors = 0
            except Exception:
                errors += 1
                log.exception("[%s] ошибка get_frame", slot.name)
                if errors >= 20:
                    log.error("[%s] захват остановлен после 20 ошибок "
                              "подряд", slot.name)
                    break
                time.sleep(0.2)
                continue

            now = time.time()

            # ---- автоэкспозиция: начало окна / фиксация значения ----
            if slot.ae_request:
                slot.ae_request = False
                self._ae_start(slot, cam)
            if slot.ae_lock_at is not None and now >= slot.ae_lock_at:
                self._ae_finish(slot, cam)

            if frame is None:
                if not slot.stalled and now - slot.last_frame_time > 3.0:
                    slot.stalled = True
                    log.warning("[%s] нет кадров от камеры более 3 с",
                                slot.name)
                continue

            if slot.stalled:
                slot.stalled = False
                log.info("[%s] кадры снова поступают", slot.name)
            slot.last_frame_time = now
            slot.frames_total += 1

            if slot.frame_size is None:
                if raw:
                    info = cam.get_info()
                    slot.frame_size = (info["width"], info["height"])
                else:
                    slot.frame_size = (frame.shape[1], frame.shape[0])

            fps_n += 1
            if now - fps_t0 >= 1.0:
                slot.current_fps = fps_n / (now - fps_t0)
                fps_n, fps_t0 = 0, now

            with slot.frame_lock:
                slot.last_frame = frame
                slot.last_frame_ts = now

            # ---- превью: декодируем/копируем не чаще preview_fps ----
            if (slot.preview_window is not None and
                    now - slot.last_preview_decode >= 1.0 / s.preview_fps):
                slot.last_preview_decode = now
                try:
                    img = cam.to_image(frame)
                except Exception:
                    img = None
                if img is not None:
                    with slot.frame_lock:
                        slot.preview_img = img
                        slot.preview_seq += 1

            # ---- запись ----
            rec = slot.recorder
            if rec is not None:
                rec.submit(frame, now,
                           1 if slot.ae_lock_at is not None else 0)
                warm, warm_n, warm_t0 = [], 0, None
            elif slot.arm_pending:
                # Меряем реальный FPS ~1 с, чтобы записать его в файл.
                # Эти кадры не теряем — уйдут в рекордер первыми.
                if warm_t0 is None:
                    warm_t0, warm, warm_n = now, [], 0
                warm_n += 1
                if len(warm) < max_warm:
                    warm.append((frame, now))
                if now - warm_t0 >= s.warmup_seconds and warm_n >= 3:
                    fps = (warm_n - 1) / (now - warm_t0)
                    self._start_slot_recorder(slot, cam, frame, fps, warm)
                    warm, warm_n, warm_t0 = [], 0, None
            else:
                warm, warm_n, warm_t0 = [], 0, None

        log.info("[%s] поток захвата завершён", slot.name)

    # ------------------------------------------------------------------
    #  Автоэкспозиция в начале каждого сегмента
    # ------------------------------------------------------------------
    @staticmethod
    def _fmt_exposure(st):
        parts = []
        if "exposure_us" in st:
            parts.append(f"экспозиция {st['exposure_us']:.0f} мкс")
        if "gain" in st:
            parts.append(f"усиление {st['gain']:.2f}")
        return ", ".join(parts) or "значения недоступны"

    def _ae_start(self, slot, cam):
        """Вызывается из потока захвата в начале сегмента."""
        s = self.settings
        secs = s.auto_exposure_seconds
        if (secs <= 0 or slot.ae_unsupported
                or not cam.supports_auto_exposure):
            return          # USB и выключенная настройка — молча
        if not cam.set_auto_exposure(True, s.auto_exposure_gain):
            slot.ae_unsupported = True
            log.warning("[%s] камера не поддерживает управление "
                        "автоэкспозицией (ExposureAuto) — пропускаю",
                        slot.name)
            return
        slot.ae_lock_at = time.time() + secs
        log.info("[%s] автоэкспозиция включена на %.0f с (начало сегмента)",
                 slot.name, secs)

    def _ae_finish(self, slot, cam):
        """Фиксирует подобранную экспозицию. Из потока захвата."""
        slot.ae_lock_at = None
        ok = cam.set_auto_exposure(False, self.settings.auto_exposure_gain)
        log.info("[%s] экспозиция %s: %s", slot.name,
                 "зафиксирована" if ok else "НЕ удалось зафиксировать",
                 self._fmt_exposure(cam.get_exposure_state()))

    def _start_slot_recorder(self, slot, cam, frame, fps, warm):
        """Вызывается из потока захвата."""
        s = self.settings
        with slot.rec_lock:
            if not slot.arm_pending:
                return
            slot.arm_pending = False
            raw = cam.frame_kind == "mjpeg_raw"
            w, h = slot.frame_size
            is_color = raw or frame.ndim == 3
            name = ((slot.camera_info or {}).get("name", slot.name)
                    + "_" + slot.name)
            fps = min(120.0, max(1.0, round(fps, 2)))
            try:
                rec = SegmentedRecorder(
                    slot.record_dir, name, w, h, fps, is_color,
                    source="mjpeg_raw" if raw else "raw",
                    codec="ffv1" if s.encoder == "ffv1" else "mjpeg",
                    min_free_mb=s.min_free_mb,
                    segment_seconds=s.segment_seconds,
                    quality=s.jpeg_quality, container=s.container,
                    queue_frames=s.queue_frames,
                    write_csv=s.write_timestamps,
                    reencode=(raw and s.encoder != "auto"),
                    on_disk_full=lambda free, sl=slot:
                        self._post(lambda: self._handle_disk_full(sl, free)),
                    on_error=lambda msg, sl=slot:
                        self._post(lambda: self._handle_rec_error(sl, msg)),
                    on_segment=lambda idx, path, sl=slot:
                        setattr(sl, "ae_request", True))
                rec.start()
            except Exception as e:
                log.error("[%s] не удалось начать запись: %s", slot.name, e)
                msg = str(e)
                self._post(lambda: self._handle_start_failed(slot, msg))
                return
            for f, ts in warm:
                rec.submit(f, ts)
            slot.recorder = rec
        log.info("[%s] запись начата: %s", slot.name, rec.segment_paths[-1])

    # ------------------------------------------------------------------
    #  Превью
    # ------------------------------------------------------------------
    def toggle_preview(self, slot_name):
        slot = self.slots[slot_name]
        if slot.camera is None:
            return
        if slot.preview_window is not None:
            self._close_preview_window(slot)
        else:
            self._open_preview_window(slot)

    def toggle_all_previews(self):
        active = self._connected_slots()
        if not active:
            return
        if any(s.preview_window is not None for s in active):
            for s in active:
                self._close_preview_window(s, silent=True)
            log.info("Все окна превью закрыты")
        else:
            for s in active:
                self._open_preview_window(s)
            log.info("Все окна превью открыты")

    def _open_preview_window(self, slot):
        if slot.camera is None:
            return
        if slot.preview_window is not None:
            slot.preview_window.lift()
            return
        info = slot.camera_info or {}
        win = tk.Toplevel(self.root)
        win.title(f"{self.get_type_display(info.get('type'))}: "
                  f"{info.get('name', slot.name)}")
        win.geometry("900x650")
        win.minsize(320, 240)
        canvas = tk.Canvas(win, background="black", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)
        canvas.create_text(12, 12, anchor=tk.NW, text="● REC", tags="rec",
                           fill="#ff3b30", font=("Arial", 16, "bold"),
                           state="hidden")
        win.bind("<Key>", self._on_key)
        win.protocol("WM_DELETE_WINDOW",
                     lambda s=slot: self._close_preview_window(s))
        slot.preview_window = win
        slot.preview_canvas = canvas
        slot.photo = None
        slot.photo_key = None
        slot.rendered_seq = -1
        slot.show_btn.config(text="Скрыть")
        log.info("[%s] окно превью открыто", slot.name)

    def _close_preview_window(self, slot, silent=False):
        win = slot.preview_window
        slot.preview_window = None
        slot.preview_canvas = None
        slot.photo = None
        slot.photo_key = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
            if not silent:
                log.info("[%s] окно превью закрыто (запись продолжается)",
                         slot.name)
        if slot.show_btn is not None and slot.camera is not None:
            slot.show_btn.config(text="Показать")

    def _render_preview(self, slot, now):
        canvas = slot.preview_canvas
        if canvas is None:
            return
        if now - slot.last_render < 1.0 / self.settings.preview_fps:
            return
        with slot.frame_lock:
            img = slot.preview_img
            seq = slot.preview_seq
        if img is None or seq == slot.rendered_seq:
            canvas.itemconfigure(
                "rec", state="normal" if slot.recorder else "hidden")
            return
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        slot.rendered_seq = seq
        slot.last_render = now

        h, w = img.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        if (nw, nh) != (w, h):
            # ужимаем ДО конвертации цвета: в 3 раза меньше данных для Mono8
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        if img.ndim == 3:
            pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        else:
            pil = Image.fromarray(img)

        key = (pil.size, pil.mode)
        x, y = (cw - nw) // 2, (ch - nh) // 2
        if slot.photo is not None and slot.photo_key == key:
            slot.photo.paste(pil)          # без пересоздания PhotoImage
            canvas.coords("frame", x, y)
        else:
            canvas.delete("frame")
            slot.photo = ImageTk.PhotoImage(pil)
            slot.photo_key = key
            canvas.create_image(x, y, anchor=tk.NW, image=slot.photo,
                                tags="frame")
            canvas.tag_raise("rec")
        canvas.itemconfigure(
            "rec", state="normal" if slot.recorder else "hidden")

    # ------------------------------------------------------------------
    #  Цикл обновления UI
    # ------------------------------------------------------------------
    def _drain_events(self):
        for _ in range(50):
            try:
                fn = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("событие UI")

    def update_ui_loop(self):
        try:
            self._drain_events()
            self._drain_log_queue()
            now = time.time()
            for slot in self.slots.values():
                self._render_preview(slot, now)

            self._tick += 1
            if self._tick % 15 == 0:          # ~2 раза в секунду
                self._refresh_labels()
            if self._tick % 60 == 0:          # ~раз в 2 секунды
                self._update_disk_label()
        except Exception:
            log.exception("Ошибка в цикле UI")
        if not self._closing:
            self.root.after(30, self.update_ui_loop)

    def _refresh_labels(self):
        for slot in self.slots.values():
            if slot.camera is None:
                continue
            fps = f"FPS: {slot.current_fps:.1f}"
            if slot.stalled:
                fps += "  (нет кадров!)"
            self._set(slot.fps_label, text=fps)

            rec = slot.recorder
            if rec is not None:
                text = (f"сегм. {rec.segment_index}, "
                        f"{int(rec.segment_remaining // 60)} мин до смены | "
                        f"записано {rec.frames_written}, "
                        f"очередь {rec.queue_size}")
                lost = rec.frames_dropped + rec.frames_corrupt
                if slot.ae_lock_at is not None:
                    text += (f" | автоэкспозиция ещё "
                             f"{max(0, int(slot.ae_lock_at - time.time()))} с")
                if lost:
                    text += f" | ПОТЕРЯНО {lost}"
                self._set(slot.rec_label, text=text,
                          foreground="#cc0000" if lost else "#007700")
            elif slot.arm_pending:
                self._set(slot.rec_label, text="подготовка записи...",
                          foreground="#555555")
            else:
                self._set(slot.rec_label, text="", foreground="#555555")
        self._refresh_controls()

    def _refresh_controls(self):
        active = self._connected_slots()
        has = bool(active)
        rec_on = self._rec_active()

        self._set(self.snapshot_btn,
                  state=tk.NORMAL if has else tk.DISABLED)
        self._set(self.record_btn,
                  state=(tk.DISABLED if (not has or self._stopping)
                         else tk.NORMAL),
                  text=("Остановка..." if self._stopping else
                        "Остановить запись" if rec_on else
                        "Записать видео"))
        any_open = any(s.preview_window is not None for s in active)
        self._set(self.preview_all_btn,
                  state=tk.NORMAL if has else tk.DISABLED,
                  text="Скрыть все" if any_open else "Показать все")
        self._set(self.dir_btn,
              state=tk.DISABLED if rec_on else tk.NORMAL)

        if rec_on:
            n = sum(1 for s in self.slots.values() if s.recorder)
            self._set(self.recording_label,
                      text=(f"Запись: ИДЁТ ({n} кам.)" if n
                            else "Запись: подготовка..."),
                      foreground="#ff0000")
            self._set(self.video_status,
                      text=f"Статус: запись идёт ({n} кам.)")
        else:
            self._set(self.recording_label, text="Запись: Нет",
                      foreground="#3010c2")

    # ------------------------------------------------------------------
    #  Диск и папка записи
    # ------------------------------------------------------------------
    def choose_record_dir(self):
        path = filedialog.askdirectory(
            title="Папка для записи видео",
            initialdir=self.settings.last_record_dir or os.path.expanduser(
                "~"))
        if path:
            self.settings.last_record_dir = path
            self.settings.save()
            self.dir_label.config(text=path)
            self._update_disk_label()
        return bool(path)

    def _update_disk_label(self):
        path = self.settings.last_record_dir or os.path.expanduser("~")
        mb = free_mb(path)
        lim = self.settings.min_free_mb
        if mb == float("inf"):
            self._set(self.disk_label, text="Диск: —",
                      foreground="#555555")
        elif mb < lim:
            self._set(self.disk_label, text=f"Диск: {mb:.0f} MB (мало!)",
                      foreground="#ff0000")
        elif mb < lim * 3:
            self._set(self.disk_label, text=f"Диск: {mb:.0f} MB",
                      foreground="#ff8800")
        else:
            self._set(self.disk_label, text=f"Диск: {mb:.0f} MB",
                      foreground="#007700")

    # ------------------------------------------------------------------
    #  Запись
    # ------------------------------------------------------------------
    def toggle_recording(self):
        if self._stopping:
            return
        if self._rec_active():
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        active = self._connected_slots()
        if not active:
            messagebox.showwarning("Предупреждение",
                                   "Сначала подключитесь к камере")
            return
        if not self.settings.last_record_dir or \
                not os.path.isdir(self.settings.last_record_dir):
            if not self.choose_record_dir():
                return
        rdir = self.settings.last_record_dir

        free = free_mb(rdir)
        if free < self.settings.min_free_mb:
            messagebox.showerror(
                "Мало места",
                f"Свободно всего {free:.0f} MB.\n"
                f"Нужно минимум {self.settings.min_free_mb} MB.")
            return
        if not os.access(rdir, os.W_OK):
            messagebox.showerror("Ошибка",
                                 f"Нет прав на запись в {rdir}")
            return

        self._disk_dialog_shown = False
        log.info("Запись → %s (кодек: %s, контейнер: %s)", rdir,
                 self.settings.encoder, self.settings.container)
        for slot in active:
            slot.record_dir = rdir
            slot.arm_pending = True       # поток захвата сам начнёт запись
        self._refresh_controls()

    def stop_recording(self):
        self._stop_all_recorders(sync=False, interactive=True)

    def _detach(self, slot):
        with slot.rec_lock:
            slot.arm_pending = False
            rec, slot.recorder = slot.recorder, None
        return rec

    def _stop_all_recorders(self, sync, interactive=False):
        pairs = []
        for slot in self.slots.values():
            rec = self._detach(slot)
            if rec is not None:
                pairs.append((slot, rec))
        if pairs:
            self._finish_recorders(pairs, sync, interactive)

    def _finish_recorders(self, pairs, sync, interactive=False):
        def work():
            res = []
            for slot, rec in pairs:
                try:
                    res.append((slot, rec, rec.stop()))
                except Exception:
                    log.exception("остановка записи %s", slot.name)
            if sync:
                self._report_stopped(res, False)
            else:
                self._post(lambda: self._on_stopped(res, interactive))

        if sync:
            work()
        else:
            self._stopping = True
            self._refresh_controls()
            Thread(target=work, daemon=True, name="stop-rec").start()

    def _report_stopped(self, res, _interactive):
        paths = []
        for slot, rec, p in res:
            paths.extend(p)
            log.info("[%s] запись остановлена: сегментов %d, кадров %d, "
                     "потеряно (очередь) %d, битых JPEG %d", slot.name,
                     len(p), rec.frames_written, rec.frames_dropped,
                     rec.frames_corrupt)
            for f in p:
                log.info("  %s", f)
        return paths

    def _on_stopped(self, res, interactive):
        self._stopping = False
        paths = self._report_stopped(res, interactive)
        self._refresh_controls()
        if not paths:
            self.video_status.config(text="Статус: запись не сохранена")
            return
        self.video_status.config(
            text=f"Статус: записано файлов: {len(paths)}")
        lost = sum(r.frames_dropped + r.frames_corrupt for _, r, _ in res)
        if interactive and not self._closing:
            folder = os.path.dirname(paths[0])
            msg = f"Всего файлов: {len(paths)}\nПапка: {folder}\n"
            if lost:
                msg += (f"\nВНИМАНИЕ: потеряно кадров: {lost} "
                        f"(см. логи).\n")
            if messagebox.askyesno("Запись завершена",
                                   msg + "\nОткрыть папку?"):
                self._open_folder(folder)

    @staticmethod
    def _open_folder(path):
        try:
            if platform.system() == "Windows":
                os.startfile(path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            log.error("Не удалось открыть папку: %s", e)

    def _handle_start_failed(self, slot, msg):
        self._refresh_controls()
        messagebox.showerror(
            "Ошибка записи",
            f"[{slot.name}] не удалось начать запись:\n{msg}")

    def _handle_rec_error(self, slot, msg):
        rec = self._detach(slot)
        if rec is not None:
            self._finish_recorders([(slot, rec)], sync=False)
        messagebox.showerror(
            "Ошибка записи",
            f"[{slot.name}] запись остановлена:\n{msg}")

    def _handle_disk_full(self, slot, free):
        rec = self._detach(slot)
        if rec is not None:
            self._finish_recorders([(slot, rec)], sync=False)
        if not self._disk_dialog_shown:
            self._disk_dialog_shown = True
            messagebox.showwarning(
                "Диск заполнен",
                f"Свободное место меньше {self.settings.min_free_mb} MB "
                f"({free:.0f} MB).\nЗапись остановлена автоматически.")

    # ------------------------------------------------------------------
    #  Снимок
    # ------------------------------------------------------------------
    def take_snapshot(self):
        active = self._connected_slots()
        if not active:
            messagebox.showwarning("Предупреждение",
                                   "Камера не подключена")
            return
        base = self.settings.last_record_dir or os.path.join(
            os.path.expanduser("~"), "dash_cam_prom")
        snap_dir = os.path.join(base, "snapshots")
        try:
            os.makedirs(snap_dir, exist_ok=True)
        except Exception as e:
            log.error("Не удалось создать %s: %s", snap_dir, e)
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        jobs = []
        for slot in active:
            with slot.frame_lock:
                frame = slot.last_frame
            if frame is None:
                continue
            name = re.sub(r"[^\w\-_.]+", "_",
                          (slot.camera_info or {}).get("name", slot.name))
            jobs.append((slot.camera, frame,
                         os.path.join(snap_dir,
                                      f"{name}_{slot.name}_{ts}")))
        if not jobs:
            log.warning("Нет кадров для снимка")
            return

        def work():
            for cam, frame, stem in jobs:
                try:
                    if cam.frame_kind == "mjpeg_raw":
                        # исходный JPEG камеры без пережатия
                        path = stem + ".jpg"
                        with open(path, "wb") as f:
                            f.write(frame.tobytes())
                    else:
                        path = stem + ".png"
                        cv2.imwrite(path, frame,
                                    [cv2.IMWRITE_PNG_COMPRESSION, 1])
                    log.info("Снимок сохранён: %s", path)
                except Exception as e:
                    log.error("Ошибка сохранения снимка: %s", e)

        Thread(target=work, daemon=True, name="snapshot").start()


def main():
    root = tk.Tk()
    app = CameraDiscoveryApp(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        log.warning("Прервано пользователем (Ctrl+C)")
        app.on_closing()
    except Exception:
        log.critical("Критическая ошибка в mainloop: %s",
                     traceback.format_exc())
        app.on_closing()


if __name__ == "__main__":
    main()
