"""Download the pinned model files (checksums verified) into a directory.

    python scripts/download_models.py ./models
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

MODELS = {
    # YuNet face detector (MIT) and SFace face recognizer (Apache-2.0), OpenCV Zoo
    "yunet.onnx": (
        "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "sface.onnx": (
        "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    # Tesseract model trained on the OCR-B passport font (BSD-3-Clause)
    "mrz.traineddata": (
        "https://github.com/DoubangoTelecom/tesseractMRZ/raw/master/tessdata_fast/mrz.traineddata",
        "ece2a54f125a73792f9cec74e6f8e62f6e5e00c7f19153ba865af689be5b5f01"),
}


def main(dest: str) -> None:
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in MODELS.items():
        path = out / name
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
            print(f"ok       {name}")
            continue
        print(f"download {name}")
        data = urllib.request.urlopen(url, timeout=120).read()  # noqa: S310 - pinned https URLs
        if hashlib.sha256(data).hexdigest() != sha:
            raise SystemExit(f"checksum mismatch for {name}")
        path.write_bytes(data)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "models")
