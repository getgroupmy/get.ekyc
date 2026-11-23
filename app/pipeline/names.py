"""Does the name the user typed match the name on the document?

Malaysian names: given names, then optionally a connector (BIN, BINTI, A/L,
A/P, @ ...) and the father's name. Users type anything from the full name to
two of their own names, with or without the connector.

Rules:
- connectors are ignored, however written (A/L, AL, A L, a/l);
- every typed word must be on the card, allowing a one-letter OCR slip
  (SIRGH for SINGH);
- at least two words, unless the card name has only one, so a common word
  alone (SINGH, MUHAMMAD) never passes;
- at least one word that is only in the person's own name (before the
  connector), so the father's name alone does not pass - not even when they
  share a word, as SINGH often is;
- every OCR reading of the card is tried, and all of them combined, because
  one pass may miss a word another one read.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

CONNECTORS = {"BIN", "BINTI", "BT", "BTE", "B", "A/L", "A/P", "AL", "AP", "S/O", "D/O", "@", "ANAK", "AK"}
_WORD_MATCH = 80  # ratio: SINGH vs SIRGH = 80


def _words(name: str) -> list[str]:
    s = name.upper().replace("A / L", "A/L").replace("A / P", "A/P")
    s = re.sub(r"\bA\s+L\b", "A/L", s)
    s = re.sub(r"\bA\s+P\b", "A/P", s)
    return [w.strip(".,") for w in re.split(r"[\s,]+", s) if w.strip(".,")]


def _split(name: str) -> tuple[list[str], list[str]]:
    """(own names, father's names), connectors removed."""
    own: list[str] = []
    father: list[str] = []
    side = own
    for w in _words(name):
        if w in CONNECTORS:
            side = father
            continue
        side.append(w)
    return own, father


def _best(word: str, pool: list[str]) -> int:
    return max((round(fuzz.ratio(word, p)) for p in pool), default=0)


def match(typed: str, readings: list[str]) -> tuple[bool, int]:
    """(passes, score 0-100) of the typed name against the best card reading."""
    t_own, t_father = _split(typed)
    typed_words = t_own + t_father
    if not typed_words:
        return False, 0
    splits = [_split(r) for r in readings if r]
    if len(splits) > 1:  # all readings combined, as one more candidate
        own_all = list(dict.fromkeys(w for o, _ in splits for w in o))
        father_all = list(dict.fromkeys(w for _, f in splits for w in f))
        splits.append((own_all, father_all))
    # Father's-name words read anywhere: a typed word that matches one of them
    # never counts as the person's own name, even if OCR garbled it elsewhere.
    father_any = list(dict.fromkeys(w for _, f in splits for w in f))
    # The two-word minimum follows the longest name read, so a cut-off reading
    # of one word cannot let a single typed word through.
    longest = max((len(o) + len(f) for o, f in splits), default=0)
    best = (False, 0)
    for c_own, c_father in splits:
        card = c_own + c_father
        if not card:
            continue
        scores = [_best(w, card) for w in typed_words]
        score = round(sum(scores) / len(scores))
        enough = len(typed_words) >= min(2, longest)
        own_only = [w for w in c_own if _best(w, c_father) < _WORD_MATCH] or c_own
        own_hit = any(_best(w, own_only) >= _WORD_MATCH and _best(w, father_any) < _WORD_MATCH
                      for w in typed_words) if own_only else True
        ok = all(sc >= _WORD_MATCH for sc in scores) and enough and own_hit
        if (ok, score) > best:
            best = (ok, score)
    return best
