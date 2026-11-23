"""Download the pinned model files (checksums verified) into a directory.

    python scripts/download_models.py ./models                    # required models
    python scripts/download_models.py ./models --with-antispoof   # + passive liveness (see licence note)
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


# Passive anti-spoofing (MiniFASNetV2-SE, 128x128, suriAI/face-antispoof-onnx).
# Code and weights are Apache-2.0, but the weights were trained on CelebA-Spoof,
# a dataset licensed for non-commercial research. Fetched only with
# --with-antispoof: decide on that licence question first.
OPTIONAL = {
    "antispoof.onnx": (
        "https://raw.githubusercontent.com/suriAI/face-antispoof-onnx/main/models/best/98.20/best_model.onnx",
        "af2381b88f38769222ed93379e12444e2a50814575de1c46170de570c55a42b6"),
}


def main(dest: str, with_antispoof: bool = False) -> None:
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    wanted = {**MODELS, **(OPTIONAL if with_antispoof else {})}
    for name, (url, sha) in wanted.items():
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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "models", with_antispoof="--with-antispoof" in sys.argv)
