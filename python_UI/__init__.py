"""
Python UI module for Dash Cam Prom
Содержит графический интерфейс пользователя
"""

# Экспортируем основной класс приложения
from .ui import CameraDiscoveryApp, main

# Определяем, что экспортируется при импорте *
__all__ = [
    'CameraDiscoveryApp',
    'main'
]

# Версия модуля (опционально)
__version__ = '1.0.0'

# Информация о модуле
__author__ = 'Your Name'
__description__ = 'GUI for Dash Cam Prom'