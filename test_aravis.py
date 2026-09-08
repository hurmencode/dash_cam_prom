import sys
import gi

gi.require_version('Aravis', '0.10')
from gi.repository import Aravis

Aravis.update_device_list()
n_devices = Aravis.get_n_devices()

for i in range(n_devices):
    device_id = Aravis.get_device_id(i)
    camera = Aravis.Camera.new(device_id)

    device = camera.get_device()

    [x,y,width,height] = camera.get_region()
    [_,ip,mask,gateway] = device.get_current_ip()

    print(f"Camera vendor: {camera.get_vendor_name()}")
    print(f"Camera model:  {camera.get_model_name()}")
    print(f"Serial number: {camera.get_device_serial_number()}")
    print(f"Camera IP:     {ip.to_string()}")
    print(f"ROI:           {x}, {y}, {width}, {height}")
    print(f"Payload:       {camera.get_payload()}")
    print(f"Pixel format:  {camera.get_pixel_format_as_string()}\n")