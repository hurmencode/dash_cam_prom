"""
Backend Dash Cam Prom: камеры, запись, настройки.
"""

from .config import Settings, load_settings
from .camera_manager import (
    CameraInterface,
    UsbCameraManager,
    ArvCameraManager,
    CameraScanner,
    create_camera,
)
from .recorder import SegmentedRecorder, free_mb

__all__ = [
    "Settings", "load_settings",
    "CameraInterface", "UsbCameraManager", "ArvCameraManager",
    "CameraScanner", "create_camera",
    "SegmentedRecorder", "free_mb",
]

__version__ = "2.0.0"
