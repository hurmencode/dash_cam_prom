"""
Backend module for Dash Cam Prom
Содержит логику работы с камерами и обработку данных
"""

# Экспортируем основные классы и функции для удобного импорта
from .camera_manager import (
    CameraInterface,
    WebcamManager,
    LucidCameraManager,
    CameraScanner,
    create_camera
)

# Определяем, что экспортируется при импорте *
__all__ = [
    'CameraInterface',
    'WebcamManager', 
    'LucidCameraManager',
    'CameraScanner',
    'create_camera'
]

# Версия модуля (опционально)
__version__ = '1.0.0'

# Информация о модуле
__author__ = 'Your Name'
__description__ = 'Camera management backend for Dash Cam Prom'