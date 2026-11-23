"""Synthetic documents for tests (no real personal data)."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.pipeline.mrz import check_digit

MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


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
        return ImageFont.load_default(size)


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
