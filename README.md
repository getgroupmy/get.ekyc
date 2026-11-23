# GET eKYC

Self-hosted identity verification for **get-ride** and any other app, over a REST API.
Runs in one Docker container on a Mac mini (Apple Silicon or Intel) or an Intel/AMD Synology NAS.
No images or personal data leave your hardware, and there are no per-check fees.

## What it verifies

| Check | How |
|---|---|
| **Document quality** | Resolution, blur, brightness and glare, with a "retake" reason for the app |
| **Document portrait** | Finds the photo on the ID, at any rotation |
| **Passport / ID data** | Reads the machine readable zone (ICAO 9303 TD1/TD2/TD3) and validates every check digit, which catches altered numbers and dates |
| **Malaysian MyKad** | Reads the NRIC number, validates the date and place-of-birth code, derives date of birth and gender |
| **Expiry and age** | Rejects expired documents and users under `EKYC_MIN_AGE` |
| **Entered data vs document** | Name, document number and date of birth the user typed are compared with the document |
| **Selfie quality** | One clear face, large enough |
| **Face match** | Selfie against the document portrait (OpenCV SFace embeddings) |
| **Liveness** | Random head-turn challenge (look straight / turn / turn the other way), each frame matched to the selfie. Optional passive anti-spoof model |
| **Duplicates** | Same document or same face already used under another user of the same app |

Each verification ends as `approved`, `rejected` (with reason codes the app can show) or `review`
(a person decides in the built-in console). Document types: `passport`, `national_id`, `driving_licence`.

## Quick start

```bash
cp .env.example .env
```

Put two secrets in `.env`:

```bash
openssl rand -hex 32      # EKYC_ADMIN_TOKEN
```

```bash
openssl rand -base64 32   # EKYC_STORAGE_KEY
```

```bash
docker compose up -d --build
```

Open `http://<host>:8080/admin`, sign in with the admin token, create an API client under
**API clients**, and copy its key. The **Try it** tab runs a verification from the browser.
Interactive API docs are at `/docs`.

**Mac mini** — install Docker Desktop or OrbStack and run the commands above. Enable "start at login"
so the service returns after a reboot (`restart: unless-stopped` handles the rest).

**Synology** — needs an Intel/AMD model with Container Manager (DSM 7.2+). Copy this folder to the NAS,
set `EKYC_DATA_PATH=/volume1/docker/ekyc` in `.env`, then create a Project in Container Manager from
`docker-compose.yml`, or run `docker compose up -d --build` over SSH. Give the container at least 1 GB RAM.
A verification took about one second on a 4-core x86 test machine; expect it to be slower on a Celeron NAS. They are processed one at a time.

**Exposing it** — the container speaks plain HTTP. Put it behind HTTPS before any phone talks to it:
Synology reverse proxy with a Let's Encrypt certificate, Caddy, Cloudflare Tunnel or Tailscale.
Only `/v1/*` needs to be reachable from your app's backend; keep `/admin` on the LAN or VPN.

## Integrating (get-ride or any app)

Call the API **from your backend**, never from the mobile app — the API key must stay server-side.
The app captures the images and sends them to your backend, which forwards them.

```bash
BASE=https://ekyc.example.com
KEY=ek_...
```

1. Create a session. `claimed` is what the user typed; `external_ref` is your user id.

```bash
curl -s -X POST $BASE/v1/verifications -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"document_type":"national_id","country":"MYS","external_ref":"driver-1042",
       "claimed":{"full_name":"Ali bin Abu","document_number":"900514-10-5231","date_of_birth":"1990-05-14"}}'
```

The response contains `id`, `required_media` and `liveness_challenge`, e.g. `["left","center","right"]`.

2. Upload each image (`document_front`, optional `document_back`, `selfie`, `liveness_1..3`).
   Each upload returns quality metrics so the app can ask for a retake immediately.

```bash
curl -s -X PUT $BASE/v1/verifications/$ID/media/document_front -H "X-API-Key: $KEY" -F file=@front.jpg
```

3. Submit, then wait for the webhook or poll.

```bash
curl -s -X POST $BASE/v1/verifications/$ID/submit -H "X-API-Key: $KEY"
```

```bash
curl -s $BASE/v1/verifications/$ID -H "X-API-Key: $KEY"
```

`status` is `processing`, then `approved` / `rejected` / `review`. `reason_details` carries a code and
a user-facing message; `result` carries every check, the extracted fields and scores.

Other endpoints: `GET /v1/verifications?external_ref=…&status=…` and
`DELETE /v1/verifications/{id}` (erases images, biometrics and extracted data).

### Liveness capture

`liveness_challenge` is a random order of `center`, `left`, `right`. Show the steps one at a time
and upload one frame per step as `liveness_1`, `liveness_2`, `liveness_3`, in that order.
`left`/`right` must be clear head turns in opposite directions (mirrored front cameras are fine);
`center` is looking at the camera. Sessions expire after `EKYC_SESSION_TTL_MINUTES`.
Send `"require_liveness": false` to skip it for a given verification.

### Webhook

If the client has a webhook URL, the result is POSTed to it (3 attempts):

```json
{"event":"verification.completed","id":"ver_…","external_ref":"driver-1042",
 "status":"approved","reasons":[],"decided_by":"auto","completed_at":"2026-11-23T01:53:50Z"}
```

Verify it before trusting it: `X-EKYC-Signature` must equal
`"sha256=" + HMAC_SHA256(webhook_secret, X-EKYC-Timestamp + "." + raw_body)`, and the timestamp should be recent.

```python
import hashlib, hmac, time

def is_valid(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature) and abs(time.time() - int(timestamp)) < 300
```

A manual decision in the console sends the same webhook with `decided_by: "admin"`.

## Decision rules

Any failed check → `rejected`. Otherwise any uncertain check → `review`. Otherwise `approved`
(or `review` when `EKYC_AUTO_APPROVE=false`). Uncertain means things like a face score between the
review and match thresholds, MRZ check digits that do not validate, a name that does not match,
a duplicate, or a document with no machine readable data and nothing typed to compare against.
All thresholds are in `.env.example`.

## Security and privacy

- Images are encrypted on disk with AES-256-GCM; extracted data, entered data and face embeddings are
  encrypted in the database. Document numbers are stored only as keyed hashes for duplicate detection.
- **Back up `EKYC_STORAGE_KEY` separately from the data folder.** Without it the data is unreadable.
- API keys are stored hashed and shown once. Clients only see their own verifications.
- Every view, decision and erasure is written to an audit trail without personal data.
- `EKYC_RETENTION_DAYS` deletes images automatically after a decision; `DELETE` erases on request.
- The container runs as a non-root user with a read-only filesystem and no capabilities.
- You are the data controller for ID and biometric data (PDPA in Malaysia). Get user consent in the app,
  set a retention period, and restrict who has the admin token.

## Limits you should know

- **Liveness** stops printed photos and still images on a screen. It does not reliably stop a replayed
  video, a deepfake injected into the camera feed, or a 3D mask. It is not iBeta / ISO 30107-3 certified.
- **Document authenticity** is checked through check digits, data consistency and the portrait.
  Holograms, fonts and other physical security features are not examined, so a well-made forgery with
  valid check digits can pass the automatic checks. Use `EKYC_AUTO_APPROVE=false` or sample reviews where that matters.
- **No registry lookups** — it does not query JPN, JPJ or immigration databases, and does no sanctions/PEP screening.
- **Driving licences and non-MRZ IDs** (other than MyKad) are verified by portrait, face match and the typed
  data appearing on the card. With nothing typed they go to manual review.
- Accuracy depends on capture quality: fill the frame with the document, avoid glare, and use the rear camera.
- Tested here with synthetic documents only. Run it against real MyKads and passports from consenting
  staff and tune the thresholds before going live.

If a regulator requires certified eKYC (for example Bank Negara for financial products), use this as the
first line and add a certified provider for those flows.

## Optional: passive anti-spoof model

The service can also score each selfie with the MobileNetV2 liveness model from
[Faceplugin's open-source SDK](https://github.com/Faceplugin-ltd/Open-Source-Face-Liveness-Detection-SDK) (MIT-0).
It is off until a converted model file exists. On any machine with Python:

```bash
git clone https://github.com/Faceplugin-ltd/Open-Source-Face-Liveness-Detection-SDK sdk
```

```bash
pip install tensorflow tf2onnx onnx opencv-python-headless
```

```bash
python scripts/convert_liveness_model.py sdk models/liveness_mnv2.onnx
```

Mount it with `- ./models/liveness_mnv2.onnx:/app/models/liveness_mnv2.onnx:ro` in `docker-compose.yml`.
`/health` then reports `"passive_liveness": true`. A spoof score above
`EKYC_PASSIVE_LIVENESS_THRESHOLD` sends the verification to review (or rejects it with
`EKYC_PASSIVE_LIVENESS_ACTION=reject`). **This path has not been run yet** — the conversion script and the
OpenCV inference need a first test, and the model's accuracy on your users is unknown, so start with `review`.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install --no-deps fastmrz==3.1.1
```

```bash
.venv/bin/python scripts/download_models.py models
```

```bash
EKYC_MODELS_DIR=models .venv/bin/python -m pytest -q
```

Tesseract must be installed (`brew install tesseract` / `apt install tesseract-ocr`).
To run without Docker: set the two secrets plus `EKYC_MODELS_DIR=models EKYC_DATA_DIR=./data`, then
`.venv/bin/uvicorn app.main:app --port 8080`.

```
app/main.py              FastAPI app, health, console
app/routers/             /v1 client API and /admin/api
app/service.py           processing, duplicates, erasure, retention
app/pipeline/            document, mrz, mykad, ocr, face, liveness, decision
app/static/admin.html    review console
```

## Components and licences

| Component | Use | Licence |
|---|---|---|
| OpenCV YuNet / SFace | Face detection / recognition | MIT / Apache-2.0 |
| [fastmrz](https://github.com/sivakumar-mahalingam/fastmrz) | MRZ location and reading | Apache-2.0 (installed without PyMuPDF, which is AGPL) |
| Tesseract + [tesseractMRZ](https://github.com/DoubangoTelecom/tesseractMRZ) | General OCR, MRZ fallback | Apache-2.0 / BSD-3 |
| Faceplugin liveness model (optional) | Passive anti-spoof | MIT-0 |

Reviewed and not used: Faceplugin's face recognition SDK (needs PyTorch and an x86-64-only binary, so it
cannot run on Apple Silicon, and has no licence file), alsenet-labs/mrz-scanner (AGPL-3.0, browser-side),
and arpiitt/AI-KYC-System (compares faces by template matching, which is not identity matching).
