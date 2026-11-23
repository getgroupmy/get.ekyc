import base64
import os
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="ekyc-test-")
os.environ.update({
    "EKYC_ADMIN_TOKEN": "test-admin-token-0123456789abcdef",
    "EKYC_STORAGE_KEY": base64.b64encode(b"k" * 32).decode(),
    "EKYC_DATA_DIR": _tmp,
    "EKYC_MODELS_DIR": os.environ.get("EKYC_MODELS_DIR", str(Path(__file__).parent.parent / "models")),
    "EKYC_PASSIVE_LIVENESS_MODEL": str(Path(_tmp) / "absent.onnx"),
    "EKYC_SANCTIONS_ENABLED": "false",  # no downloads in tests; test_risk turns it on with a fixture list
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN = {"X-Admin-Token": os.environ["EKYC_ADMIN_TOKEN"]}


@pytest.fixture(scope="session")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def api_key(client):
    r = client.post("/admin/api/clients", json={"name": "get-ride"}, headers=ADMIN)
    assert r.status_code == 201
    return {"X-API-Key": r.json()["api_key"]}


@pytest.fixture(scope="session")
def need_models():
    from app.pipeline import face, ocr
    if not (face.models_available() and ocr.available()):
        pytest.skip("face models / tesseract not installed")
