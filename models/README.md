# Models

Object detection ("Object detect" recording mode) needs a YOLOv8-format ONNX model at
`models/yolov8n.onnx` (path configurable as `yolo_model` in `config.json`). The weights are
not committed: they are a 13 MB binary, and Ultralytics YOLOv8 weights are licensed
AGPL-3.0, which matters if you distribute this firmware. Any YOLOv8-format ONNX export
with the 80 COCO classes works, so a permissively licensed model can be swapped in.

Export yolov8n once, in a throwaway environment (PyTorch is not needed at runtime):

```sh
python3 -m venv /tmp/yolo-export && . /tmp/yolo-export/bin/activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install ultralytics onnx onnxslim
yolo export model=yolov8n.pt format=onnx imgsz=640 opset=12 simplify=False
cp yolov8n.onnx /path/to/camera_firmware/models/
```

Without the file, a camera set to "Object detect" falls back to motion detection and the
API refuses to save that mode.
