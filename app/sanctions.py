"""Sanctions screening against the UN Security Council Consolidated List.

BNM's AML/CFT policy (para 27) requires screening every customer (name, ID
number, date of birth) against the UNSC list and the domestic MOHA list, and
re-screening the whole base whenever a list changes. The UN list is public XML
(https://scsanctions.un.org/resources/xml/en/consolidated.xml) and is fetched
daily. The MOHA domestic list is only published as PDF; drop a CSV of it
(name,date_of_birth,reference) at <data>/sanctions/moha.csv to include it.

A hit is a *possible* match for a person to resolve, never an automatic
rejection: names collide often.
"""
from __future__ import annotations

import csv
import logging
import re
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz

from .config import get_settings

log = logging.getLogger("ekyc.sanctions")
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
_lock = threading.Lock()
_cache: dict = {"mtime": None, "entries": []}


@dataclass(frozen=True)
class Entry:
    source: str
    reference: str
    names: tuple[str, ...]
    birth_years: tuple[int, ...]


def _dir() -> Path:
    d = get_settings().data_dir / "sanctions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^A-Z ]", " ", name.upper()).split())


def parse_un(xml_bytes: bytes) -> list[Entry]:
    root = ET.fromstring(xml_bytes)
    out = []
    for ind in root.iter("INDIVIDUAL"):
        def t(tag: str) -> str:
            el = ind.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        main = " ".join(x for x in (t("FIRST_NAME"), t("SECOND_NAME"), t("THIRD_NAME"), t("FOURTH_NAME")) if x)
        names = [main] if main else []
        for alias in ind.findall("INDIVIDUAL_ALIAS"):
            a = alias.find("ALIAS_NAME")
            if a is not None and a.text and a.text.strip():
                names.append(a.text.strip())
        years = set()
        for dob in ind.findall("INDIVIDUAL_DATE_OF_BIRTH"):
            for tag in ("DATE", "YEAR", "FROM_YEAR", "TO_YEAR"):
                el = dob.find(tag)
                if el is not None and el.text:
                    m = re.match(r"(\d{4})", el.text.strip())
                    if m:
                        years.add(int(m.group(1)))
        if names:
            out.append(Entry("UN", t("REFERENCE_NUMBER") or t("DATAID"), tuple(_norm(n) for n in names if _norm(n)),
                             tuple(sorted(years))))
    return out


def _parse_csv(path: Path, source: str) -> list[Entry]:
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            name = _norm(row.get("name", ""))
            if not name:
                continue
            m = re.match(r"(\d{4})", (row.get("date_of_birth") or "").strip())
            out.append(Entry(source, row.get("reference", ""), (name,), (int(m.group(1)),) if m else ()))
    return out


def entries() -> list[Entry]:
    """The loaded lists (re-read when the files change)."""
    d = _dir()
    files = [p for p in (d / "un.xml", d / "moha.csv") if p.exists()]
    stamp = tuple((p.name, p.stat().st_mtime) for p in files)
    with _lock:
        if _cache["mtime"] != stamp:
            loaded: list[Entry] = []
            for p in files:
                try:
                    loaded += parse_un(p.read_bytes()) if p.suffix == ".xml" else _parse_csv(p, "MOHA")
                except Exception:  # noqa: BLE001
                    log.exception("could not read sanctions list %s", p)
            _cache.update(mtime=stamp, entries=loaded)
        return _cache["entries"]


def status() -> dict:
    d = _dir()
    un = d / "un.xml"
    return {"enabled": get_settings().sanctions_enabled, "entries": len(entries()),
            "un_list_updated": datetime.fromtimestamp(un.stat().st_mtime, timezone.utc).isoformat() if un.exists() else None,
            "moha_csv": (d / "moha.csv").exists()}


def refresh(force: bool = False) -> bool:
    """Download the UN list if older than a day. True when it changed."""
    import httpx
    s = get_settings()
    if not s.sanctions_enabled:
        return False
    path = _dir() / "un.xml"
    if not force and path.exists() and datetime.now().timestamp() - path.stat().st_mtime < 86400:
        return False
    try:
        r = httpx.get(s.sanctions_un_url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        parse_un(r.content)  # refuse to replace a good list with a broken one
    except Exception as exc:  # noqa: BLE001
        log.warning("UN sanctions list not refreshed: %s", exc)
        return False
    changed = not path.exists() or path.read_bytes() != r.content
    path.write_bytes(r.content)
    return changed


def screen(full_name: str | None, date_of_birth: str | None, threshold: int = 90) -> list[dict]:
    """Possible matches for a person: the name (any order, any alias) close
    enough, and the birth year not contradicting the list when both are known."""
    if not full_name:
        return []
    name = _norm(re.sub(r"\b(BIN|BINTI|BT|A/L|A/P|AL|AP)\b", " ", full_name.upper()))
    year = int(date_of_birth[:4]) if date_of_birth and re.match(r"\d{4}", date_of_birth) else None
    hits = []
    for e in entries():
        score = max((fuzz.token_sort_ratio(name, n) for n in e.names), default=0)
        if score < threshold:
            continue
        if year and e.birth_years and all(abs(year - y) > 1 for y in e.birth_years):
            continue
        hits.append({"source": e.source, "reference": e.reference, "score": round(score), "listed_name": e.names[0]})
    return sorted(hits, key=lambda h: -h["score"])[:5]


def is_stale(hours: int = 48) -> bool:
    p = _dir() / "un.xml"
    return not p.exists() or datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, timezone.utc) > timedelta(hours=hours)
