"""Проверка: видит ли Aravis камеры. Запуск: python3 tools/check_aravis.py"""
import gi

Aravis = None
for ver in ("0.10", "0.8"):
    try:
        gi.require_version("Aravis", ver)
        from gi.repository import Aravis
        print(f"Aravis {ver}")
        break
    except (ValueError, ImportError):
        continue
if Aravis is None:
    raise SystemExit("Aravis не найден: sudo apt install gir1.2-aravis-0.8")

Aravis.update_device_list()
n_devices = Aravis.get_n_devices()
print(f"Найдено устройств: {n_devices}\n")

for i in range(n_devices):
    camera = Aravis.Camera.new(Aravis.get_device_id(i))
    x, y, width, height = camera.get_region()
    print(f"Vendor:        {camera.get_vendor_name()}")
    print(f"Model:         {camera.get_model_name()}")
    print(f"Serial:        {camera.get_device_serial_number()}")
    if camera.is_gv_device():
        _, ip, _mask, _gw = camera.get_device().get_current_ip()
        print(f"IP:            {ip.to_string()}")
    print(f"ROI:           {x}, {y}, {width}, {height}")
    print(f"Payload:       {camera.get_payload()}")
    print(f"Pixel format:  {camera.get_pixel_format_as_string()}\n")
