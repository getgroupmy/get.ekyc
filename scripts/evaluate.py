"""Measure get.ekyc's accuracy on a folder of real (consented) samples.

Runs every sample through the same checks as the service, in this process only:
nothing is uploaded, stored in the database or sent anywhere. Then compares the
result with the decision a person made and with the true values typed into
labels.csv, and works out how the face threshold trades false accepts against
false rejects.

Folder layout (see samples.example/ for a template):

    samples/
      labels.csv
      case01/front.jpg  [back.jpg]  selfie.jpg  [liveness_1.jpg liveness_2.jpg liveness_3.jpg]
      case02/...

labels.csv columns (only case_id, document_type and expected are required):

    case_id          folder name
    person_id        same value for cases of the same person (default: case_id);
                     the different-people face test pairs approved cases with
                     different person_id, so give a person's repeat visits one id
    document_type    passport | national_id | driving_licence
    country          ISO alpha-3, e.g. MYS
    expected         what a person decided: approve | reject
    full_name, document_number, date_of_birth (YYYY-MM-DD), expiry_date, address
                     the true values from the document, for field accuracy;
                     also sent as what the user "typed", like the app does
    liveness_order   e.g. center,left,right - the order the frames were taken in

Run:

    EKYC_MODELS_DIR=models python scripts/evaluate.py samples --out eval-out

Writes eval-out/results.csv and eval-out/report.md. By default neither contains
names, numbers or dates from the documents, only whether they were right; add
--show-values to include them (then treat the output like the samples).
"""
from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("EKYC_ADMIN_TOKEN", "evaluation-only-token-0000000000")
os.environ.setdefault("EKYC_STORAGE_KEY", base64.b64encode(b"e" * 32).decode())
os.environ.setdefault("EKYC_DATA_DIR", "/tmp/ekyc-evaluate")
# Judge the engine's own verdict, not the "a person approves everything" policy.
os.environ["EKYC_AUTO_APPROVE"] = "true"

import numpy as np  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

FIELDS = ("full_name", "document_number", "date_of_birth", "expiry_date", "address")
DOC_TYPES = ("passport", "national_id", "driving_licence")
THRESHOLDS = (0.25, 0.30, 0.33, 0.363, 0.40, 0.45, 0.50, 0.55, 0.60)


@dataclass
class Case:
    case_id: str
    person_id: str
    document_type: str
    country: str | None
    expected: str
    truth: dict
    liveness_order: list[str] | None
    folder: Path


@dataclass
class Outcome:
    case: Case
    status: str = "error"
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    extracted: dict = field(default_factory=dict)
    face_score: float | None = None
    seconds: float = 0.0
    error: str | None = None
    selfie_emb: np.ndarray | None = None
    doc_emb: np.ndarray | None = None


def _norm_id(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _read_value(o: "Outcome", name: str) -> str | None:
    """labels.csv has one document_number column: it may hold the person's ID
    number or the document's own number, so either reading counts."""
    if name != "document_number":
        return o.extracted.get(name)
    for key in ("document_number", "id_number"):
        if o.extracted.get(key) and field_correct(name, o.case.truth[name], o.extracted[key]):
            return o.extracted[key]
    return o.extracted.get("id_number") or o.extracted.get("document_number")


def field_correct(name: str, truth: str, read: str | None) -> bool | None:
    """None when the engine did not read the field at all."""
    if not read:
        return None
    if name == "full_name":
        return fuzz.token_set_ratio(truth.upper(), read.upper()) >= 90
    if name == "address":
        return fuzz.token_set_ratio(truth.upper(), read.upper()) >= 85
    if name == "document_number":
        return _norm_id(truth) == _norm_id(read)
    return truth.strip() == read.strip()


def load_cases(root: Path) -> list[Case]:
    labels = root / "labels.csv"
    if not labels.exists():
        raise SystemExit(f"{labels} not found")
    cases = []
    with labels.open(newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=2):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            cid = row.get("case_id")
            if not cid:
                continue
            problems = []
            if row.get("document_type") not in DOC_TYPES:
                problems.append(f"document_type must be one of {DOC_TYPES}")
            if row.get("expected") not in ("approve", "reject"):
                problems.append("expected must be approve or reject")
            folder = root / cid
            if not (folder / "front.jpg").exists() and not any(folder.glob("front.*")):
                problems.append("front.jpg missing")
            if not any(folder.glob("selfie.*")):
                problems.append("selfie.jpg missing")
            if problems:
                print(f"labels.csv line {i} ({cid}): skipped - {'; '.join(problems)}")
                continue
            order = [s.strip() for s in row.get("liveness_order", "").split(",") if s.strip()] or None
            cases.append(Case(
                case_id=cid, person_id=row.get("person_id") or cid, document_type=row["document_type"],
                country=(row.get("country") or "").upper() or None, expected=row["expected"],
                truth={k: row[k] for k in FIELDS if row.get(k)}, liveness_order=order, folder=folder))
    return cases


def _file(folder: Path, stem: str) -> bytes | None:
    for p in sorted(folder.glob(f"{stem}.*")):
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            return p.read_bytes()
    return None


def run_case(case: Case) -> Outcome:
    from app.pipeline import document, images, runner
    from app.pipeline import face as facelib

    out = Outcome(case)
    media = {"document_front": _file(case.folder, "front"), "selfie": _file(case.folder, "selfie")}
    if back := _file(case.folder, "back"):
        media["document_back"] = back
    challenge = None
    if case.liveness_order:
        frames = [_file(case.folder, f"liveness_{i + 1}") for i in range(len(case.liveness_order))]
        if all(frames):
            challenge = case.liveness_order
            media.update({f"liveness_{i + 1}": f for i, f in enumerate(frames)})
    claimed = {k: v for k, v in case.truth.items() if k in ("full_name", "document_number", "date_of_birth", "address")}
    t = time.perf_counter()
    try:
        r = runner.run(media, case.document_type, case.country, claimed or None, challenge)
        out.status, out.reasons, out.extracted = r.status, r.reasons, r.extracted
        out.checks = {c["name"]: c for c in r.checks}
        # A case without head-turn frames can never pass the auto-approval gate on
        # liveness alone; judge the rest of it as it would go with them.
        gate = out.checks.get("auto_approval", {})
        if challenge is None and out.status == "review" and out.reasons == ["manual_approval_required"] \
                and gate.get("detail", {}).get("missing") == ["liveness"]:
            out.status, out.reasons = "approved", ["liveness_not_in_case"]
        out.face_score = r.scores.get("face_match")
        out.selfie_emb = r.selfie_embedding
        front = images.decode(media["document_front"])
        img, f = document._find_portrait(front)
        out.doc_emb = facelib.embed(img, f) if f is not None else None
    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"
    out.seconds = time.perf_counter() - t
    return out


def _pct(n: int, d: int) -> str:
    return "—" if d == 0 else f"{100 * n / d:.0f}% ({n}/{d})"


def face_rates(outs: list[Outcome]) -> tuple[list[float], list[float]]:
    """Genuine: each approved case's selfie vs its own document. Impostor: an
    approved case's selfie vs another person's document photo. Only cases a person
    approved are used, because there the document and the selfie are known to
    show the same person; a rejected case may mix two people."""
    ok = [o for o in outs if o.case.expected == "approve"]
    genuine = [o.face_score for o in ok if o.face_score is not None]
    impostor = []
    for a in ok:
        if a.selfie_emb is None:
            continue
        for b in ok:
            if b.doc_emb is not None and b.case.person_id != a.case.person_id:
                impostor.append(float(np.dot(a.selfie_emb, b.doc_emb)))
    return genuine, impostor


def report(outs: list[Outcome], show_values: bool) -> str:
    from app.config import get_settings

    s = get_settings()
    L: list[str] = []
    n = len(outs)
    L.append("# get.ekyc accuracy report\n")
    L.append(f"{n} cases · face threshold {s.face_match_threshold} · review band from {s.face_review_threshold}\n")
    if n < 50:
        L.append(f"> Only {n} cases: treat every percentage below as a rough sign, not a rate.\n")

    # 1. decisions ------------------------------------------------------------
    L.append("## 1. Engine verdict vs the person's decision\n")
    L.append("| Person decided | approved | review | rejected | error |\n|---|---|---|---|---|")
    for exp in ("approve", "reject"):
        row = [o for o in outs if o.case.expected == exp]
        cnt = {k: sum(1 for o in row if (o.status if not o.error else "error") == k)
               for k in ("approved", "review", "rejected", "error")}
        L.append(f"| {exp} ({len(row)}) | {cnt['approved']} | {cnt['review']} | {cnt['rejected']} | {cnt['error']} |")
    good = [o for o in outs if o.case.expected == "approve"]
    bad = [o for o in outs if o.case.expected == "reject"]
    L.append("")
    L.append(f"- **Wrongly approved** (should be rejected, engine approved): {_pct(sum(o.status == 'approved' for o in bad), len(bad))}")
    L.append(f"- **Wrongly rejected** (should be approved, engine rejected): {_pct(sum(o.status == 'rejected' for o in good), len(good))}")
    L.append(f"- **Left for a person** (review): {_pct(sum(o.status == 'review' for o in outs), n)}")
    L.append(f"- **Decided automatically and correctly**: "
             f"{_pct(sum(o.status == 'approved' for o in good) + sum(o.status == 'rejected' for o in bad), n)}\n")

    # 2. reasons --------------------------------------------------------------
    L.append("## 2. Why cases were not approved\n")
    counts: dict[str, list[int]] = {}
    for o in outs:
        for r in o.reasons:
            c = counts.setdefault(r, [0, 0])
            c[0 if o.case.expected == "approve" else 1] += 1
    if counts:
        L.append("| Reason | on genuine cases | on cases a person rejected |\n|---|---|---|")
        for r, (g, b) in sorted(counts.items(), key=lambda kv: -sum(kv[1])):
            L.append(f"| `{r}` | {g} | {b} |")
        L.append("\nA reason that fires often on genuine cases is costing manual reviews; look at those cases first.\n")
    else:
        L.append("None.\n")

    # 3. fields ---------------------------------------------------------------
    L.append("## 3. Fields read from the document\n")
    L.append("Correct = matches labels.csv. Not read = the engine found nothing for that field.\n")
    L.append("| Document | Field | Correct | Wrong | Not read |\n|---|---|---|---|---|")
    for dt in DOC_TYPES:
        sub = [o for o in outs if o.case.document_type == dt]
        for f in FIELDS:
            vals = [field_correct(f, o.case.truth[f], _read_value(o, f)) for o in sub if f in o.case.truth]
            if not vals:
                continue
            ok, wrong, none = vals.count(True), vals.count(False), vals.count(None)
            L.append(f"| {dt} | {f} | {_pct(ok, len(vals))} | {wrong} | {none} |")
    L.append("")

    # 4. face -----------------------------------------------------------------
    L.append("## 4. Face match\n")
    no_face_doc = sum(1 for o in outs if o.checks.get("document_portrait", {}).get("status") == "fail")
    no_face_selfie = sum(1 for o in outs if o.checks.get("selfie_quality", {}).get("code") == "selfie_no_face")
    L.append(f"- No face found on the document: {_pct(no_face_doc, n)}; in the selfie: {_pct(no_face_selfie, n)}")
    genuine, impostor = face_rates(outs)
    if genuine:
        L.append(f"- Same person ({len(genuine)} pairs): median {statistics.median(genuine):.3f}, "
                 f"lowest {min(genuine):.3f}")
    if impostor:
        L.append(f"- Different people ({len(impostor)} pairs): median {statistics.median(impostor):.3f}, "
                 f"highest {max(impostor):.3f}")
    if not impostor:
        L.append("- Different people: none to compare. This needs approved cases from at least two people"
                 " (different person_id).")
    if genuine and impostor:
        L.append("\n| Threshold | Same person rejected (false reject) | Different people accepted (false accept) |\n|---|---|---|")
        for t in THRESHOLDS:
            fr = sum(g < t for g in genuine)
            fa = sum(i >= t for i in impostor)
            mark = " ← current" if abs(t - s.face_match_threshold) < 1e-9 else ""
            L.append(f"| {t:.3f}{mark} | {_pct(fr, len(genuine))} | {_pct(fa, len(impostor))} |")
        safe = min((t for t in THRESHOLDS if all(i < t for i in impostor)), default=None)
        L.append("")
        if safe is not None:
            L.append(f"Lowest threshold in the table with no false accepts in this sample: **{safe:.3f}**. "
                     f"With {len(impostor)} impostor pairs that only shows the false-accept rate is "
                     f"probably below about {300 / len(impostor):.1f}% (rule of three), not that it is zero. "
                     "Do not lower the threshold below 0.363 on fewer than about 1,000 different-people pairs.")
        L.append("Change it with EKYC_FACE_MATCH_THRESHOLD; cases between EKYC_FACE_REVIEW_THRESHOLD and it go to review.\n")

    # 5. liveness -------------------------------------------------------------
    live = [o for o in outs if "liveness" in o.checks]
    if live:
        L.append("## 5. Liveness\n")
        lg = [o for o in live if o.case.expected == "approve"]
        lb = [o for o in live if o.case.expected == "reject"]
        L.append(f"- Genuine people failed: {_pct(sum(o.checks['liveness']['status'] != 'pass' for o in lg), len(lg))}")
        L.append(f"- Cases a person rejected that passed liveness: "
                 f"{_pct(sum(o.checks['liveness']['status'] == 'pass' for o in lb), len(lb))}\n")

    # 6. speed and errors -----------------------------------------------------
    times = [o.seconds for o in outs if not o.error]
    L.append("## 6. Speed and errors\n")
    if times:
        L.append(f"- Median {statistics.median(times):.2f} s per case, slowest {max(times):.2f} s")
    errs = [o for o in outs if o.error]
    L.append(f"- Crashed: {len(errs)}" + (" — " + "; ".join(f"{o.case.case_id}: {o.error}" for o in errs[:5]) if errs else ""))

    # 7. worst cases ----------------------------------------------------------
    wrong = [o for o in outs if (o.case.expected == "reject" and o.status == "approved")
             or (o.case.expected == "approve" and o.status == "rejected")]
    if wrong:
        L.append("\n## 7. Cases to look at first\n")
        for o in wrong:
            L.append(f"- `{o.case.case_id}`: person said {o.case.expected}, engine {o.status} "
                     f"({', '.join(o.reasons) or 'no reasons'}); face {o.face_score}")
    if not show_values:
        L.append("\n_No document values are included. Run with --show-values to add them to results.csv._")
    return "\n".join(L) + "\n"


def write_results(outs: list[Outcome], path: Path, show_values: bool) -> None:
    cols = ["case_id", "person_id", "document_type", "expected", "status", "agrees", "reasons", "face_score",
            "seconds", "error"] + [f"{f}_correct" for f in FIELDS]
    if show_values:
        cols += [f"{f}_read" for f in FIELDS]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for o in outs:
            agrees = (o.case.expected == "approve" and o.status == "approved") or \
                     (o.case.expected == "reject" and o.status == "rejected")
            row = {"case_id": o.case.case_id, "person_id": o.case.person_id, "document_type": o.case.document_type,
                   "expected": o.case.expected, "status": o.status if not o.error else "error",
                   "agrees": "review" if o.status == "review" else agrees, "reasons": " ".join(o.reasons),
                   "face_score": "" if o.face_score is None else o.face_score, "seconds": f"{o.seconds:.2f}",
                   "error": o.error or ""}
            for f in FIELDS:
                c = field_correct(f, o.case.truth[f], _read_value(o, f)) if f in o.case.truth else ""
                row[f"{f}_correct"] = "not_read" if c is None else c
                if show_values:
                    row[f"{f}_read"] = _read_value(o, f) or ""
            w.writerow(row)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("samples", type=Path)
    ap.add_argument("--out", type=Path, default=Path("eval-out"))
    ap.add_argument("--show-values", action="store_true", help="include values read from documents in results.csv")
    args = ap.parse_args(argv)

    cases = load_cases(args.samples)
    if not cases:
        print("no usable cases")
        return 1
    outs = []
    for i, c in enumerate(cases, 1):
        o = run_case(c)
        outs.append(o)
        print(f"[{i}/{len(cases)}] {c.case_id}: {o.status if not o.error else 'error'}", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    write_results(outs, args.out / "results.csv", args.show_values)
    (args.out / "report.md").write_text(report(outs, args.show_values), encoding="utf-8")
    print(f"\nWrote {args.out / 'report.md'} and {args.out / 'results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
