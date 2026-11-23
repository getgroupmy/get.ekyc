"""One-off: convert the Faceplugin open-source liveness model (Keras .hdf5) to ONNX.

The service runs the ONNX file through OpenCV, so TensorFlow is only needed for
this conversion, never at runtime (TensorFlow does not run on many Synology CPUs).

Source model (MIT-0): https://github.com/Faceplugin-ltd/Open-Source-Face-Liveness-Detection-SDK

    pip install tensorflow tf2onnx onnx opencv-python-headless
    python scripts/convert_liveness_model.py /path/to/sdk-checkout app/assets/liveness_mnv2.onnx
"""
from __future__ import annotations

import os
import sys


def main(sdk_dir: str, out_path: str) -> None:
    sys.path.insert(0, sdk_dir)
    import cv2
    import numpy as np
    import tensorflow as tf
    import tf2onnx
    from liveness_detector import LivenessDetector

    det = LivenessDetector(os.path.join(sdk_dir, "weights", "liveness.hdf5"))
    for name in ("real", "fake"):
        img = cv2.imread(os.path.join(sdk_dir, "input", f"{name}.jpg"))
        print(f"keras  {name}: spoof score {det.predict(img).score:.4f}")

    spec = (tf.TensorSpec((1, 224, 224, 3), tf.float32, name="input"),)
    tf2onnx.convert.from_keras(det.model, input_signature=spec, opset=13, output_path=out_path)
    print(f"wrote {out_path} ({os.path.getsize(out_path) / 1e6:.1f} MB)")

    net = cv2.dnn.readNetFromONNX(out_path)
    for name in ("real", "fake"):
        img = cv2.imread(os.path.join(sdk_dir, "input", f"{name}.jpg"))
        batch = det.preprocess(img)  # NHWC float32 RGB 0..1
        net.setInput(batch)
        print(f"opencv {name}: spoof score {float(np.squeeze(net.forward())):.4f}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
