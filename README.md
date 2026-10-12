# 🎥 Видеорегистратор 3000

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![OpenCV](https://img.shields.io/badge/OpenCV-4.8+-green.svg)](https://opencv.org/)
[![Aravis](https://img.shields.io/badge/Aravis-0.10-orange.svg)](https://wiki.gnome.org/Projects/Aravis)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 📖 Описание

**Видеорегистратор 3000** — десктопное приложение для работы с промышленными GigE Vision камерами (Lucid Triton, Hikrobot MV-CS) и обычными USB-камерами (UVC). Поддерживает **одновременную запись с двух камер** — одна GigE через Aravis, одна USB через OpenCV.

Возможности:
- 🔍 **Сканирование** GigE-камер в подсети (Aravis) и USB-камер (`/dev/video*`)
- 📷 **Подключение** одной или двух камер одновременно
- 🎬 **Просмотр** видео в реальном времени в сетке 2×1 (GigE | USB)
- 🎥 **Одновременная запись** с двух камер в отдельные файлы
- ✂️ **Сегментация записи по 1 часу** — длинные записи не растягиваются в один файл
- 📸 **Снимки** в PNG с каждой активной камеры
- 📊 **Отслеживание** FPS по каждой камере и свободного места на диске
- 📋 **Логи** всех операций с датой/временем — в UI и в файл

## ✨ Особенности

- **Mono8 без лишних конвертаций** — кадр идёт из Aravis в `VideoWriter` напрямую, `cvtColor` делается только для предпросмотра.
- **Два независимых слота** — GigE и USB работают параллельно, у каждой свой поток захвата, свой recorder, свой сегмент записи.
- **Сегментация по 1 часу** — каждые 60 минут текущий файл закрывается, открывается новый. При сбое питания теряется максимум один текущий сегмент.
- **Отображение отключается на время записи** — CPU полностью уходит под приём и сжатие кадров.
- **Защита от заполнения диска** — при свободном месте ниже порога (по умолчанию 5000 МБ) запись останавливается автоматически, UI получает уведомление.
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
- **v4l-utils** (для идентификации USB-камер через `v4l2-ctl`)

### Linux (Ubuntu 22.04 / 24.04, Jetson Orin Nano)

```bash
sudo apt update
sudo apt install -y \
    python3-gi python3-gi-cairo gir1.2-aravis-0.10 \
    libaravis-0.10-0 arv-tools \
    python3-opencv python3-pil python3-numpy \
    v4l-utils

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
    git build-essential \
    meson ninja-build pkg-config \
    libglib2.0-dev libxml2-dev \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    libgirepository1.0-dev gobject-introspection \
    libusb-1.0-0-dev zlib1g-dev \
    gtk-doc-tools xsltproc \
    libgtk-3-dev libnotify-dev
```

#### Сборка
```bash

git clone https://github.com/AravisProject/aravis.git
cd aravis
git checkout 0.10.0     # или нужный тег

meson setup build
ninja -C build
sudo ninja -C build install
```

#### Проверка
```bash

python3 -c "import gi; gi.require_version('Aravis','0.10'); from gi.repository import Aravis; print('Aravis OK')"
```
Если при проверке выдает ошибку, то выпоните следующую команду:

```bash

find /usr/local/ -name "Aravis*"
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
|   ├── recordings/           # Видеофайлы MJPG
|   ├── snapshots/            # PNG-снимки
│   ├── __init__.py
│   └── ui.py                 # Tkinter-приложение
├── logs/                     # Файлы логов сессий (создаётся автоматически)
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
##### Через ярлык на рабочем столе (Linux)

В корне репозитория есть файлы ```dash_cam.desktop``` и ```run.sh```.
на срабочий стол скопируйте файл ```dash_cam.desktop``` и разрешите запуск при помощи команд:

```
bash

cp dash_cam.desktop ~/Desktop/
chmod +x ~/Desktop/dash_cam.desktop
```

Затем на рабочем столе кликните по иконке правой кнопкой мыши и выберите «Allow Launching» / «Разрешить запуск» (это стандартное требование GNOME для .desktop файлов).

При первом двойном клике приложение запустится автоматически. Если в будущем захотите, чтобы ярлык появился в меню приложений:
```
bash

cp dash_cam.desktop ~/.local/share/applications/
```

#### Windows (MSYS2):
```bash

cd /path/to/dash_cam_prom
python python_UI/ui.py
```

### Основные функции

- Поиск камер — сканирует GigE-подсеть через Aravis (broadcast discovery) и USB-камеры через /dev/video*.
- Подключиться — выбирает одну или две камеры (Ctrl+Click для нескольких). GigE-камера идёт в слот gige, USB — в слот usb. При подключении новой камеры предыдущая освобождается. Автоматически переключается на вкладку «Видео» и запускает поток.
- Остановить / Запустить видео — управляет превью без переподключения камер.
- Записать видео — стартует запись на всех активных камерах одновременно. Каждая камера пишет в свой файл. Во время записи предпросмотр отключается, чтобы не конкурировать за CPU.
- Снимок — сохраняет кадр с каждой активной камеры в отдельный PNG (Mono8 или BGR — по формату кадра).
- Логи — все действия пишутся в UI и в файл logs/session_*.log.

### Поведение при записи

- Перед стартом записи проверяется свободное место на диске. Если меньше MIN_FREE_MB (5000 МБ) — запись не начнётся.
- Во время записи свободное место проверяется раз в секунду каждым рекордером независимо. Если у одной камеры места не хватит — её запись остановится, вторая продолжит.
- Индикатор «Диск: N MB» в шапке меняет цвет: зелёный (> 15000 МБ), оранжевый (5000–15000 МБ), красный (< 5000 МБ).
- По завершении записи предпросмотр возвращается.
- Сегментация: каждые 60 минут текущий файл закрывается, открывается новый. Имена: <camera>_part001_FFV1.avi, _part002_, и т.д.
- Кодеки: MJPG. Итоговый кодек пишется в лог.
- USB-камеры пишутся как BGR (3 канала), GigE — как Mono8 (2 канала). Это определяется автоматически по формату кадра..

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
- Один слот на тип камеры. В текущей версии можно одновременно писать с одной GigE и одной USB-камеры. Больше — не поддерживается.
- Драйвер Realtek r8168 не тянет Jumbo Frames (MTU 9000) — используйте MTU 1500.
- Максимальный FPS камеры Hikrobot MV-CS050-60GM — 23 fps (паспортный), на ROI 1936×1464 даёт ~29 fps за счёт меньшего потока данных.

### 📄 Лицензия

Проект распространяется под лицензией MIT. Использует Aravis (LGPL-2.1-or-later) как динамическую библиотеку — это не накладывает ограничений на лицензию приложения.