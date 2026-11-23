"""Synthetic documents for tests (no real personal data)."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.pipeline.mrz import check_digit

from pathlib import Path


def _first(*paths: str) -> str:
    """First font that exists. Passports print the MRZ in a monospace font
    (OCR-B); drawing it in a proportional one makes the reader drop letters such
    as I, so the tests need a real monospace font on every platform."""
    return next((p for p in paths if Path(p).exists()), paths[0])


MONO = _first(
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",  # Debian / Ubuntu
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",  # macOS
    "/System/Library/Fonts/Menlo.ttc",
    "/Library/Fonts/Courier New Bold.ttf",
)
SANS = _first(
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def td3_lines(surname="ERIKSSON", given="ANNA MARIA", number="L898902C3", country="UTO",
              dob="740812", sex="F", expiry="320415", personal="ZE184226B<<<<<") -> tuple[str, str]:
    l1 = f"P<{country}{surname}<<{given.replace(' ', '<')}".ljust(44, "<")[:44]
    body = f"{number}{check_digit(number)}{country}{dob}{check_digit(dob)}{sex}{expiry}{check_digit(expiry)}{personal}{check_digit(personal)}"
    composite = body[0:10] + body[13:20] + body[21:43]
    return l1, body + check_digit(composite)


def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        import pytest
        pytest.skip(f"no usable font for the synthetic documents ({path}); install fonts-liberation or fonts-dejavu")


def face_bgr() -> np.ndarray:
    from skimage import data
    return cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)


def passport_image(lines: tuple[str, str], portrait_bgr: np.ndarray | None, text: list[str] | None = None) -> bytes:
    w, h = 1500, 1050
    im = Image.new("RGB", (w, h), (236, 232, 222))
    d = ImageDraw.Draw(im)
    d.text((60, 40), "PASSPORT   UTOPIA", font=_font(SANS, 44), fill=(40, 40, 60))
    if portrait_bgr is not None:
        p = Image.fromarray(cv2.cvtColor(portrait_bgr, cv2.COLOR_BGR2RGB)).resize((420, 520))
        im.paste(p, (70, 160))
    for i, t in enumerate(text or []):
        d.text((560, 180 + i * 70), t, font=_font(SANS, 40), fill=(20, 20, 20))
    for i, line in enumerate(lines):
        d.text((60, 850 + i * 80), line, font=_font(MONO, 50), fill=(10, 10, 10))
    arr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    return cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def card_image(portrait_bgr: np.ndarray | None, text: list[str]) -> bytes:
    w, h = 1300, 820
    im = Image.new("RGB", (w, h), (225, 235, 240))
    d = ImageDraw.Draw(im)
    if portrait_bgr is not None:
        p = Image.fromarray(cv2.cvtColor(portrait_bgr, cv2.COLOR_BGR2RGB)).resize((380, 470))
        im.paste(p, (850, 180))
    for i, t in enumerate(text):
        d.text((60, 60 + i * 90), t, font=_font(SANS, 48), fill=(15, 15, 15))
    arr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    return cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def jpeg(bgr: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


def selfie(variant: int = 0) -> np.ndarray:
    """Same person as the portrait, but a different crop / lighting."""
    img = face_bgr()[0:330, 80:400]
    img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.convertScaleAbs(cv2.GaussianBlur(img, (3, 3), 0), alpha=0.9, beta=8 + variant)


def _guilloche(w: int, h: int) -> Image.Image:
    """Pale cyan card with dense wavy security lines, like a MyKad's background."""
    im = Image.new("RGB", (w, h), (206, 236, 242))
    d = ImageDraw.Draw(im)
    xs = np.arange(0, w, 3)
    for k in range(0, h + 200, 9):
        ys = k + 18 * np.sin(xs / 37.0 + k / 23.0) + 7 * np.sin(xs / 11.0)
        d.line(list(zip(xs.tolist(), ys.tolist())), fill=(110, 185, 205), width=1)
    for k in range(-h, w, 14):
        d.line([(k, 0), (k + h // 2, h)], fill=(150, 205, 220), width=1)
    return im


def mykad_front(number: str, name_lines: list[str], address_lines: list[str], sex_word: str,
                portrait_bgr: np.ndarray | None) -> bytes:
    w, h = 1500, 950
    im = _guilloche(w, h)
    d = ImageDraw.Draw(im)
    d.text((180, 40), "KAD PENGENALAN", font=_font(SANS, 52), fill=(30, 30, 40))
    d.text((180, 100), "MALAYSIA", font=_font(SANS, 70), fill=(30, 30, 40))
    d.text((90, 240), number, font=_font(SANS, 62), fill=(60, 60, 60))
    if portrait_bgr is not None:
        p = Image.fromarray(cv2.cvtColor(portrait_bgr, cv2.COLOR_BGR2RGB)).resize((400, 480))
        im.paste(p, (1000, 260))
    y = 600
    for line in name_lines:
        d.text((90, y), line, font=_font(SANS, 40), fill=(15, 15, 15)); y += 46
    for line in address_lines:
        d.text((90, y), line, font=_font(SANS, 34), fill=(15, 15, 15)); y += 40
    d.text((1060, 780), "WARGANEGARA", font=_font(SANS, 36), fill=(15, 15, 15))
    d.text((1160, 825), sex_word, font=_font(SANS, 36), fill=(15, 15, 15))
    return cv2.imencode(".jpg", cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def mykad_back(number: str) -> bytes:
    im = _guilloche(1500, 950)
    ImageDraw.Draw(im).text((430, 680), f"{number}-06-01", font=_font(SANS, 58), fill=(70, 70, 70))
    return cv2.imencode(".jpg", cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
