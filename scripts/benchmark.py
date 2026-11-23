"""Measure how many checks per minute this machine can do.

In the running container (NAS or Mac mini). It needs one photo with a face in it,
any selfie will do, copied into the data folder as bench-face.jpg:

    docker compose exec -e BENCH_FACE=/data/bench-face.jpg ekyc python scripts/benchmark.py

From a checkout with requirements-dev.txt installed (uses a built-in test photo):

    EKYC_MODELS_DIR=models python scripts/benchmark.py

Uses synthetic documents (no personal data). Prints the time per check with one
worker, and the ceiling with one worker per CPU core.
"""
from __future__ import annotations

import base64
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("EKYC_ADMIN_TOKEN", "benchmark-only-token-000000000000")
os.environ.setdefault("EKYC_STORAGE_KEY", base64.b64encode(b"b" * 32).decode())
os.environ.setdefault("EKYC_DATA_DIR", "/tmp/ekyc-benchmark")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

ROUNDS = int(os.environ.get("ROUNDS", "8"))


def _inputs():
    from PIL import Image, ImageDraw, ImageFont

    from app.pipeline.mrz import check_digit

    try:
        from skimage import data
        portrait = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)
    except ImportError:  # the container has no scikit-image: use any face photo
        portrait = cv2.imread(os.environ.get("BENCH_FACE", "face.jpg"))
        if portrait is None:
            raise SystemExit("install scikit-image, or set BENCH_FACE to a photo with one face")

    def font(size, mono=False):
        candidates = (
            ["/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/System/Library/Fonts/Menlo.ttc"]
            if mono else
            ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Helvetica.ttc"])
        for p in candidates:
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
        return ImageFont.load_default(size)

    n, dob, exp, pers = "L898902C3", "740812", "320415", "ZE184226B<<<<<"
    l1 = "P<UTOERIKSSON<<ANNA<MARIA".ljust(44, "<")
    body = f"{n}{check_digit(n)}UTO{dob}{check_digit(dob)}F{exp}{check_digit(exp)}{pers}{check_digit(pers)}"
    l2 = body + check_digit(body[0:10] + body[13:20] + body[21:43])

    def doc(lines, text, size):
        im = Image.new("RGB", size, (236, 232, 222))
        d = ImageDraw.Draw(im)
        im.paste(Image.fromarray(cv2.cvtColor(portrait, cv2.COLOR_BGR2RGB)).resize((420, 520)), (70, 160))
        for i, t in enumerate(text):
            d.text((560, 180 + i * 70), t, font=font(40), fill=(20, 20, 20))
        for i, t in enumerate(lines):
            d.text((60, 850 + i * 80), t, font=font(50, True), fill=(10, 10, 10))
        return cv2.imencode(".jpg", cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR))[1].tobytes()

    selfie = cv2.resize(portrait[0:330, 80:400], (1080, 1110))
    jpg = lambda a: cv2.imencode(".jpg", a)[1].tobytes()  # noqa: E731
    return {
        "passport": doc((l1, l2), ["PASSPORT"], (1500, 1050)),
        "mykad": doc((), ["KAD PENGENALAN MALAYSIA", "900514-10-5232", "ANNA BINTI ERIK", "SELANGOR"], (1300, 1050)),
        "selfie": jpg(selfie),
        "frames": [jpg(selfie)] * 3,
    }


def _tasks(inp):
    from app.pipeline import face, images, liveness, runner

    def compare():  # what get-ride's face-compare asks for: two photos, one score
        a, b = images.decode(inp["passport"]), images.decode(inp["selfie"])
        face.similarity(face.embed(a, face.detect(a)[0]), face.embed(b, face.detect(b)[0]))

    def passport():
        runner.run({"document_front": inp["passport"], "selfie": inp["selfie"]}, "passport", None,
                   {"full_name": "Anna Eriksson"}, None)

    def passport_live():
        media = {"document_front": inp["passport"], "selfie": inp["selfie"]}
        media.update({f"liveness_{i + 1}": f for i, f in enumerate(inp["frames"])})
        runner.run(media, "passport", None, None, ["center", "left", "right"])

    def mykad():
        runner.run({"document_front": inp["mykad"], "selfie": inp["selfie"]}, "national_id", "MYS",
                   {"full_name": "Anna binti Erik", "document_number": "900514105232"}, None)

    _ = liveness
    return {
        "Face compare (2 photos, /compare)": compare,
        "Passport + selfie (full check)": passport,
        "Passport + selfie + 3 liveness frames": passport_live,
        "MyKad + selfie (full check)": mykad,
    }


def _time(name: str, threads: int | None) -> float:
    if threads:
        cv2.setNumThreads(threads)
    fn = _tasks(_inputs())[name]
    fn()  # warm up: loads the models
    times = []
    for _ in range(ROUNDS):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return statistics.median(times)


def main() -> None:
    import platform
    cores = os.cpu_count() or 1
    print(f"{platform.processor() or platform.machine()}  ·  {cores} cores  ·  OpenCV {cv2.__version__}\n")
    print(f"{'check':40s} {'1 check':>8s} {'per min':>8s} {'1 core':>8s} {'per min, ' + str(cores) + ' workers':>18s}")
    names = list(_tasks(_inputs()))
    shared = {n: _time(n, None) for n in names}  # one worker, OpenCV on all cores (how the service runs)
    os.environ["OMP_THREAD_LIMIT"] = "1"  # Tesseract, which runs as a child process
    single = {n: _time(n, 1) for n in names}  # one core only
    for n in names:
        print(f"{n:40s} {shared[n]:7.2f}s {60 / shared[n]:8.0f} {single[n]:7.2f}s {cores * 60 / single[n]:18.0f}")
    print("\n'per min' is the service as shipped (one worker). The last column is the ceiling with one"
          " worker per core, before memory (about 370 MB per worker) and disk become the limit.")


if __name__ == "__main__":
    main()
