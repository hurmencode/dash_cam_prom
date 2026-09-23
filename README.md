# 🎥 Видеорегистратор 3000

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![OpenCV](https://img.shields.io/badge/OpenCV-4.8+-green.svg)](https://opencv.org/)
[![Aravis](https://img.shields.io/badge/Aravis-0.10-orange.svg)](https://wiki.gnome.org/Projects/Aravis)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 📖 Описание

**Dash Cam Prom** — десктопное приложение для работы с промышленными GigE Vision камерами. Поддерживает камеры Lucid Triton и Hikrobot MV-CS через единый интерфейс на базе Aravis.

Возможности:
- 🔍 **Сканирование** доступных GigE-камер в подсети
- 📷 **Подключение** к выбранной камере
- 🎬 **Просмотр** видео в реальном времени с OSD (FPS, REC-таймер)
- 📸 **Снимки** в формате PNG (Mono8)
- 🎥 **Запись** видео в MJPG (Mono8)
- 📊 **Отслеживание** FPS и свободного места на диске
- 📋 **Логи** всех операций с датой/временем — в UI и в файл

## ✨ Особенности

- **Mono8 без лишних конвертаций** — кадр идёт из Aravis в `VideoWriter` напрямую, `cvtColor` делается только для предпросмотра.
- **Отображение отключается на время записи** — CPU полностью уходит под приём и сжатие кадров.
- **Защита от заполнения диска** — при свободном месте ниже порога (по умолчанию 1000 МБ) запись останавливается автоматически, UI получает уведомление.
- **Логи дублируются в файл** — все сообщения пишутся в `~/dash_cam_prom/logs/session_<дата>_<время>.log` с `flush()`. Даже при segfault в C-библиотеке история сохранится на диске.
- **Обработка аварийных завершений** — Ctrl+C, SIGTERM, необработанные исключения в main и в фоновых потоках корректно логируются и сохраняются.
- **Освобождение камеры при переключении** — вторая камера не делит с первой гигабитный линк.
- **Только один Tkinter-поток** — все обращения к UI идут из main-потока через `root.after`.

## 🚀 Установка

### Требования

- **Python** 3.12+
- **OpenCV** 4.8+
- **Aravis** 0.10+ (с GObject Introspection)
- **Pillow**
- **NumPy**
- **PyGObject** (`gi`)

### Linux (Ubuntu 22.04 / 24.04, Jetson Orin Nano)

```bash
sudo apt update
sudo apt install -y \
    python3-gi python3-gi-cairo gir1.2-aravis-0.10 \
    libaravis-0.10-0 arv-tools \
    python3-opencv python3-pil python3-numpy
```

Проверка, что Aravis виден из Python:

```bash
python3 -c "import gi; gi.require_version('Aravis','0.10'); from gi.repository import Aravis; print('Aravis OK')"
```
### Windows (MSYS2)

```bash
pacman -S \
    mingw-w64-x86_64-python \
    mingw-w64-x86_64-python-pip \
    mingw-w64-x86_64-opencv \
    mingw-w64-x86_64-python-numpy \
    mingw-w64-x86_64-python-pillow \
    mingw-w64-x86_64-python-gobject \
    mingw-w64-x86_64-aravis
```

### Виртуальное окружение
```bash
git clone https://github.com/YOUR_USERNAME/dash_cam_prom.git
cd dash_cam_prom

python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# или
source .venv/Scripts/activate   # Windows (MSYS2)

pip install -r requirements.txt
```

 ### 🧱 Сборка Aravis из исходников (опционально)

Если в репозитории твоего дистрибутива нет Aravis 0.10 или нужна свежая версия — можно собрать из исходников. Это занимает 5–10 минут.
#### Зависимости
```bash

sudo apt install -y \
    meson ninja-build pkg-config \
    libglib2.0-dev libxml2-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    libgirepository1.0-dev gobject-introspection \
    libusb-1.0-0-dev libzlib-dev \
    gtk-doc-tools
```

#### Сборка
```bash

git clone https://github.com/AravisProject/aravis.git
cd aravis
git checkout 0.10.0     # или нужный тег

meson setup build \
    -Ddocumentation=disabled \
    -Dtests=disabled \
    -Dviewer=disabled \
    -Dgst-plugin=disabled
ninja -C build
sudo ninja -C build install
```

#### Проверка
```bash

python3 -c "import gi; gi.require_version('Aravis','0.10'); from gi.repository import Aravis; print('Aravis OK')"
```

Когда это реально нужно:
- Нужна определенная версия (например 0.8 или 0.10)
- Нужны свежие багфиксы (например, для конкретной GigE-камеры).
- Нужны опции, которых нет в пакетной сборке.

Оговорки
- На Jetson Orin Nano пакетный Aravis 0.10 из Ubuntu 24.04 работает стабильно — собирать из исходников не требуется.
- Сборка из исходников не даёт ничего сверх пакетной, если у тебя нет конкретной причины.

## 📁 Структура проекта
```text
dash_cam_prom/
├── backend/
│   ├── __init__.py
│   └── camera_manager.py     # Aravis-обёртка + VideoRecorder + Scanner
├── python_UI/
│   ├── __init__.py
│   └── ui.py                 # Tkinter-приложение
├── logs/                     # Файлы логов сессий (создаётся автоматически)
├── recordings/               # Видеофайлы MJPG
├── snapshots/                # PNG-снимки
├── .gitignore
├── README.md
├── requirements.txt
└── LICENSE
```

## 🎯 Использование
### Запуск

#### Linux / Jetson:
```bash

cd /path/to/dash_cam_prom
python3 python_UI/ui.py
```

#### Windows (MSYS2):
```bash

cd /c/dash_cam_prom
python python_UI/ui.py
```

### Основные функции

- Поиск камер — сканирует GigE-подсеть через Aravis (broadcast discovery).
- Подключиться — создаёт ArvCamera, настраивает Mono8, выделяет буферы, запускает acquisition. При подключении новой камеры предыдущая освобождается.
- Показать видео — включает предпросмотр (960×540, с OSD: FPS и REC-таймер).
- Записать видео — запись в .avi (MJPG, isColor=False). Во время записи предпросмотр отключается, чтобы не конкурировать за CPU.
- Снимок — сохраняет текущий кадр в PNG (Mono8).
- Логи — все действия пишутся в UI и в файл logs/session_*.log.

### Поведение при записи

- Перед стартом записи проверяется свободное место на диске. Если меньше MIN_FREE_MB (1000 МБ) — запись не начнётся.
- Во время записи свободное место проверяется раз в секунду. Если просело ниже порога — запись останавливается автоматически, в UI выводится предупреждение.
- Индикатор «Диск: N MB» в шапке меняет цвет: зелёный (> 1500 МБ), оранжевый (500–1500 МБ), красный (< 500 МБ).
- По завершении записи предпросмотр возвращается, если пользователь его включал.

### Клавиши быстрого доступа

Планируется: 
- Space (старт/стоп видео), 
- R (старт/стоп записи), 
- S (снимок), 
- L (логи).

### 🔧 Настройка сети для GigE

Для стабильной работы GigE-камеры на 30 fps желательно:
- MTU 1500 (Jumbo Frames 9000 на Realtek r8168 работают нестабильно — проверено).
- Увеличенные буферы сокета:

```bash
sudo sysctl -w net.core.rmem_max=33554432
sudo sysctl -w net.core.rmem_default=33554432
sudo sysctl -w net.core.netdev_max_backlog=5000
```
- Отдельная подсеть для камеры, статический IP на интерфейсе Jetson.
- Освобождение камеры при переключении на другую — иначе обе делят гигабит.

### Диагностика

Запуск с включённой статистикой Aravis:
```bash

ARV_DEBUG=stream python3 python_UI/ui.py
```

В конце сессии в консоль выводится:

- n_missing_packets — потерянные пакеты (должно быть 0)

- n_missing_frames — потерянные кадры (норма 0–1 за сессию)

- n_resend_requests — запросы переотправки

- n_ignored_packets — проигнорированные «хвосты» (не потери)

### 📝 Логи

Все действия пишутся:

- в UI — во вкладке «Логи» с цветовой индикацией (синий/зелёный/оранжевый/красный),
- в файл — ~/dash_cam_prom/logs/session_<YYYYMMDD_HHMMSS>.log с flush().

Формат строки:
```text

[2026-09-23 10:55:08] Успешно подключено к TRI028S-M (IP: 192.168.0.41)
```

Логи сохраняются при:

- обычном закрытии окна,
- Ctrl+C (SIGINT),
- SIGTERM (kill),
- необработанных исключениях в main-потоке,
- необработанных исключениях в фоновых потоках.

**Что НЕ спасает**: kill -9 (SIGKILL) и segfault в C-библиотеках — но файл-дублёр сохраняет всё, что успело записаться.

### 🛠️ Известные ограничения

- GigE Vision не поддерживает одновременную работу двух SDK (Aravis + MVS) с одной камерой. Используется только Aravis.
- При работе с двумя камерами одновременно через один гигабитный порт будут потери пакетов. Освобождайте предыдущую камеру перед подключением новой.
- Драйвер Realtek r8168 не тянет Jumbo Frames (MTU 9000) — используйте MTU 1500.
- Максимальный FPS камеры Hikrobot MV-CS050-60GM — 23 fps (паспортный), на ROI 1936×1464 даёт ~29 fps за счёт меньшего потока данных.

### 📄 Лицензия

Проект распространяется под лицензией MIT. Использует Aravis (LGPL-2.1-or-later) как динамическую библиотеку — это не накладывает ограничений на лицензию приложения.