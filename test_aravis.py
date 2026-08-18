import sys
import gi

gi.require_version('Aravis', '0.8')
from gi.repository import Aravis

try:
    if len(sys.argv) > 1:
        camera = Aravis.Camera.new(sys.argv[1])
    else:
        camera = Aravis.Camera.new(None)
except TypeError:
    print("Cameras not found")


[x,y,width,height] = camera.get_region()

print(f"Camera vendor: {camera.get_vendor_name()}")
print(f"Camera model:  {camera.get_model_name()}")
print(f"Camera IP:     {Aravis.get_device_address(3)}")
print(f"ROI:           {x}, {y}, {width}, {height}")
print(f"Payload:       {camera.get_payload()}")
print(f"Pixel format:  {camera. get_pixel_format_as_string()}")