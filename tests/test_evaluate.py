import csv

import numpy as np

from scripts import evaluate
from tests import synth


def _case(root, cid, front, selfie):
    d = root / cid
    d.mkdir()
    (d / "front.jpg").write_bytes(front)
    (d / "selfie.jpg").write_bytes(selfie)


def test_evaluate_end_to_end(tmp_path, need_models):
    selfie = synth.jpeg(synth.selfie())
    _case(tmp_path, "good", synth.passport_image(synth.td3_lines(), synth.face_bgr()), selfie)
    _case(tmp_path, "expired", synth.passport_image(synth.td3_lines(number="E22222222", expiry="200101"),
                                                    synth.face_bgr()), selfie)
    _case(tmp_path, "noface", synth.passport_image(synth.td3_lines(number="N33333333"), synth.face_bgr()),
          synth.jpeg(np.full((800, 800, 3), 128, np.uint8)))
    with (tmp_path / "labels.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "person_id", "document_type", "expected", "full_name", "document_number",
                    "date_of_birth", "expiry_date"])
        w.writerow(["good", "anna", "passport", "approve", "Anna Maria Eriksson", "L898902C3", "1974-08-12", "2032-04-15"])
        w.writerow(["expired", "anna2", "passport", "reject", "", "E22222222", "", "2020-01-01"])
        w.writerow(["noface", "anna3", "passport", "reject", "", "", "", ""])
        w.writerow(["broken", "x", "passport", "maybe", "", "", "", ""])  # skipped: bad label, no folder

    out = tmp_path / "out"
    assert evaluate.main([str(tmp_path), "--out", str(out)]) == 0
    rows = {r["case_id"]: r for r in csv.DictReader((out / "results.csv").open())}
    assert set(rows) == {"good", "expired", "noface"}
    assert rows["good"]["status"] == "approved" and rows["good"]["agrees"] == "True"
    assert rows["good"]["document_number_correct"] == "True" and rows["good"]["full_name_correct"] == "True"
    assert rows["expired"]["status"] == "rejected" and "document_expired" in rows["expired"]["reasons"]
    assert rows["noface"]["status"] == "rejected"
    assert "document_number_read" not in rows["good"]  # no document values without --show-values
    text = (out / "report.md").read_text()
    for heading in ("## 1.", "## 3.", "## 4.", "Wrongly approved", "Same person", "none to compare"):
        assert heading in text
    assert "L898902C3" not in text and "ERIKSSON" not in text.upper()


def test_field_correct():
    assert evaluate.field_correct("full_name", "Ali bin Abu", "ALI BIN ABU") is True
    assert evaluate.field_correct("document_number", "900514-10-5231", "900514105231") is True
    assert evaluate.field_correct("date_of_birth", "1990-05-14", "1990-05-15") is False
    assert evaluate.field_correct("expiry_date", "2030-01-01", None) is None
