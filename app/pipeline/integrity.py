"""Image integrity checks every major provider runs: the same photo reused
across accounts, a "selfie" that is really a copy of the ID photo, and images
whose metadata says they went through an editor.

None of these reject on their own: each sends the case to a person.
"""
from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from .checks import PASS, REVIEW, check

# Editing tools whose name lands in EXIF "Software" / XMP CreatorTool.
_EDITORS = ("PHOTOSHOP", "GIMP", "LIGHTROOM", "SNAPSEED", "PICSART", "CANVA", "PIXLR", "AFFINITY", "PAINT.NET",
            "FACETUNE", "PHOTOPEA", "PHOTODIRECTOR", "MEITU", "AIRBRUSH", "REMINI", "FOTOR", "INKSCAPE",
            "CORELDRAW", "ILLUSTRATOR", "PHOTOROOM")


def phash(data: bytes) -> str:
    """64-bit perceptual hash (DCT): survives resizing and recompression."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return ""
    small = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    low = cv2.dct(small)[:8, :8]
    bits = (low > np.median(low[1:].ravel() if low.size > 1 else low)).ravel()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


SAME_IMAGE = 6  # bits of 64: the same picture, recompressed or resized


def editor_used(data: bytes) -> str | None:
    """Name of an image editor recorded in the file's metadata, if any."""
    try:
        im = Image.open(io.BytesIO(data))
        exif = im.getexif()
        texts = [str(exif.get(0x0131, "")), str(exif.get(0x010E, ""))]  # Software, ImageDescription
        xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
        if isinstance(xmp, bytes):
            xmp = xmp.decode("utf-8", "ignore")
        texts.append(str(xmp)[:4000])
    except Exception:  # noqa: BLE001
        return None
    joined = " ".join(texts).upper()
    return next((e.title() for e in _EDITORS if e in joined), None)


def edited_check(media: dict[str, bytes]) -> dict:
    found = {k: e for k, d in media.items() if (e := editor_used(d))}
    if found:
        return check("image_integrity", REVIEW, "image_edited", editors=found)
    return check("image_integrity", PASS)


# A real new selfie of the same person matches the ID photo at roughly 0.4-0.85
# (SFace cosine). A photo or screenshot of the card's own portrait scores ~0.95+.
COPY_OF_ID_PHOTO = 0.93


def selfie_copy_check(similarity: float) -> dict | None:
    if similarity >= COPY_OF_ID_PHOTO:
        return check("selfie_source", REVIEW, "selfie_is_id_photo", similarity=round(similarity, 4))
    return None
