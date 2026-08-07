# 🎥 Dash Cam Prom - Видеорегистратор 3000

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![OpenCV](https://img.shields.io/badge/OpenCV-4.8+-green.svg)](https://opencv.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 📖 Описание

**Dash Cam Prom** — это десктопное приложение для работы с камерами (веб-камеры и промышленные GigE камеры Lucid). Приложение позволяет:
- 🔍 **Сканировать** доступные камеры
- 📷 **Подключаться** к камерам (USB веб-камеры и GigE через Arena API)
- 🎬 **Просматривать** видео в реальном времени
- 📸 **Делать снимки** в формате PNG
- 🎥 **Записывать видео** с выбором качества (сжатый / без потерь)
- 📊 **Отслеживать FPS** в реальном времени
- 📋 **Вести логи** всех операций

## 🚀 Установка

### Требования

- **Python** 3.12 или выше
- **OpenCV** 4.8+
- **Arena SDK** (для камер Lucid)
- **Pillow** (для работы с изображениями)

### Установка через MSYS2 (рекомендуется для Windows)

```bash
# Установка через MSYS2
pacman -S \
    mingw-w64-x86_64-python \
    mingw-w64-x86_64-python-pip \
    mingw-w64-x86_64-opencv \
    mingw-w64-x86_64-python-numpy \
    mingw-w64-x86_64-python-pillow \
    mingw-w64-x86_64-python-gobject \
    mingw-w64-x86_64-aravis
```

### Установка через pip
```bash
# Клонируйте репозиторий
git clone https://github.com/YOUR_USERNAME/dash_cam_prom.git
cd dash_cam_prom

# Создайте виртуальное окружение
python -m venv .venv
source .venv/Scripts/activate  # Windows
# или
source .venv/bin/activate      # Linux

# Установите зависимости
pip install -r requirements.txt
```

### 📁 Структура проекта
```
dash_cam_prom/
├── backend/              # Бэкенд-логика
│   ├── __init__.py
│   └── camera_manager.py # Управление камерами
├── python_UI/            # Пользовательский интерфейс
│   ├── __init__.py
│   ├── ui.py            # Основное приложение
│   └── favicon.png      # Иконка
├── recordings/           # Папка для записей
├── snapshots/            # Папка для снимков
├── .gitignore
├── README.md
├── requirements.txt
└── LICENSE
```

### 🎯 Использование

#### Запуск приложения

Windows (MSYS2):

``` bash
cd /c/dash_cam_prom
python python_UI/ui.py
```

Windows (командная строка):

``` cmd
cd C:\dash_cam_prom
python python_UI\ui.py
```

Linux/Mac:

```bash
cd /path/to/dash_cam_prom
python3 python_UI/ui.py
```

### Основные функции
- Поиск камер — сканирует доступные USB и GigE камеры

- Подключение — подключается к выбранной камере

- Просмотр — показывает видео в реальном времени

- Запись видео:

  - Сжатый (XVID) — хорошее качество, маленький размер

  - Без потерь (FFV1) — идеальное качество, большой размер

  - Кадры PNG — каждый кадр сохраняется как PNG

- Снимки — сохраняет текущий кадр в PNG

- Логи — все действия записываются в лог

### Клавиши быстрого доступа (планируется)
- Space — Старт/Стоп видео

- R — Старт/Стоп записи

- S — Сделать снимок

- L — Показать логи

### 📝 Логи

Все действия записываются во вкладке "Логи":

- 🔵 Информационные сообщения

- 🟢 Успешные операции

- 🟠 Предупреждения

- 🔴 Ошибки