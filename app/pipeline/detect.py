"""What is this document? One image in, type / country / card family out.
Nothing is stored. Used by the console when a file is picked and by apps that
want to know what a user uploaded before starting a verification."""
from __future__ import annotations

from . import catalog, document, images


def detect(data: bytes) -> dict:
    img = images.decode(data)
    res = document.analyze(img, None, "auto", None, {})
    ex = res.extracted
    doc_type = ex.get("document_type", "national_id")
    meta = catalog.TYPES.get(doc_type, catalog.TYPES["national_id"])
    data_check = next((c for c in res.checks if c["name"] == "document_data"), {})
    return {
        "document_type": doc_type,
        "name": meta.name,
        "category": meta.category,
        "subtype": ex.get("document_subtype"),
        "country": ex.get("country"),
        "how": ex.get("document_type_source"),
        "needs_selfie": meta.needs_selfie,
        "needs_back": doc_type == "national_id" and ex.get("country") == "MYS",
        "readable": data_check.get("status") == "pass",
        "issue": data_check.get("code"),
        "not_document": data_check.get("code") == "not_a_document",
    }
