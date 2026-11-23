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
| **Malaysian MyKad** | Removes the blue security pattern, reads the IC number several times on both sides and votes; date and state of birth and gender come from the number and are checked against LELAKI/PEREMPUAN and the back of the card. Also reads the name, citizenship and address (the religion some cards print is not read: PDPA sensitive data) |
| **Issuing country** | Detected from the image: the MRZ issuing state, a MyKad number, or the printed wording (MYS, SGP, IDN, THA, PHL, BRN, IND, BGD, NPL, MMR, VNM, CHN, PAK). The `country` you send is optional; if it disagrees, the case goes to review |
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

Open `http://localhost:8080/admin` on the host, sign in with the admin token, create an API client
under **API clients**, and copy its key. The **Try it** tab runs a verification from the browser.
Interactive API docs are at `/docs`. The port is published on localhost only; change it to
`"8080:8080"` in `docker-compose.yml` to reach the console from other LAN machines.

### On the GET Synology (DS224+)

This follows the same pattern as get-ride's `deploy/face-server`, which already runs on that NAS
(Celeron J4125, x86-64, no AVX, Container Manager, DSM 7.2+):

1. In File Station create `docker/ekyc` on volume1. Only `administrators` should have access to
   the `docker` share. Synology's Docker does not create bind-mounted folders with the right owner,
   so make the data folder over SSH and hand it to the container's user (uid 10001):

   ```bash
   cd /volume1/docker/ekyc && mkdir -p data && sudo chown 10001 data && chmod 700 data
   ```

2. Copy this repository's files into `/volume1/docker/ekyc`. Copy `.env.example` to `.env` there, then
   fill in `EKYC_ADMIN_TOKEN`, `EKYC_STORAGE_KEY`, `EKYC_DATA_PATH=/volume1/docker/ekyc/data` and
   `TUNNEL_TOKEN`. Type the secrets on the NAS; never send them through chat, email or git.
3. In Cloudflare Zero Trust, create a tunnel (for example `getride-ekyc`) and add a public hostname
   such as `ekyc.getride.my` with service **HTTP** `ekyc:8080`. Copy its token into `TUNNEL_TOKEN`.
4. Container Manager, **Project**, **Create**: path `/volume1/docker/ekyc`, source `docker-compose.yml`.
   To include the tunnel, run `docker compose --profile tunnel up -d --build` over SSH instead, because
   the Project screen does not pass `--profile`.
5. Check `https://ekyc.getride.my/health`. It should return `"status": "ok"` and `"model": "SFace"`.

No router port is opened, and Cloudflare provides HTTPS. Block `/admin*` at Cloudflare, with an Access
policy or a WAF rule for that path, so the console is reachable only on the NAS itself or through
Cloudflare Access. In tests here a verification took about 1 s and peaked at about 370 MB of memory;
the compose file caps the container at 1 GB. Verifications run one at a time.

Follow get-ride's rule from `docs/NAS.md`: the app must keep working when the NAS is off. Treat
this service as a check that can be unavailable, not a hard dependency of sign-up or going online.

**Mac mini**: install Docker Desktop or OrbStack, run the same commands, and enable "start at login".

### Relationship to get-ride's face-server

`deploy/face-server` (live at `face.getride.my`) only compares two photos. This service does the
same comparison with the same models and threshold, and adds document reading, liveness, duplicates
and a review queue. It also serves face-server's API unchanged:

- `POST /compare` with header `x-api-key` and body `{"image1": "<base64>", "image2": "<base64>"}`
  returns `{"similarity", "model": "SFace", "verified", "cosine"}`. `similarity` uses the same
  0 to 1 scale, where the 0.363 cosine threshold shows as 80%.
- `422 {"error":"no_face"}`, `400 {"error":"bad_image"}`, `413 {"error":"too_large"}`, `401`.
- `GET /health` returns `model`, and returns 401 when a wrong `x-api-key` is sent.
- Photos sent to `/compare` are compared in memory and never stored.

To switch get-ride's existing `face-compare` edge function to this service without changing app
code, create an API client here, then in GET-Ride go to **Admin, Settings, API Keys, Face match
server** and set it to `https://ekyc.getride.my:<ek_ key>`. Both services can run side by side
until you retire face-server. **This switch has not been tried against the live function yet:**
`/compare` was built to the contract described in that session, not tested against
`supabase/functions/_shared/face_server.ts`, which this repository cannot see.

### What get-ride already has, and what this adds

Checked against `getgroupmy/get-ride` main on 23 Nov 2026. get-ride already runs sign-in checks
(`identity_checks`, migration 0281, Admin › Documents › Sign-in checks):

| Step | get-ride today | get.ekyc |
|---|---|---|
| Live selfie | On the phone with ML Kit (eyes open or head turn); only `liveness = passed` and `blink`/`turn` reach the server | Checks the head-turn frames on the server, so a modified app cannot just claim "passed" |
| Reading the ID | Phone ML Kit, then the NAS `doc-reader` (RapidOCR), then Google Vision (`id-read`) → `read_name`, `read_ic`, `read_expiry` | Validates what is read: MRZ check digits on passports, MyKad date and place-of-birth code, expiry, minimum age |
| Name / IC against the profile | `mismatch` (`name`, `ic`) | Same idea, with fuzzy name matching |
| Selfie against ID photo | `face-compare`: NAS face-server, then AWS, then in-browser → `face_score`, `face_state` | Same models and threshold, plus `/compare` so it can be the NAS step |
| Same person or ID on two accounts | Not checked | Duplicate document and duplicate face across users |
| Decision | Admin approves or rejects; the score is advisory, except the rider ID gate (0343), which auto-verifies a rider above the country's `autoVerifyScore` | Same by default (`EKYC_AUTO_APPROVE=false`). `/compare` keeps face-server's score scale exactly, because that gate reads it |

The rule in get-ride's `docs/NAS.md` applies: get.ride must keep working when the NAS is off, so
everything here is a second opinion that can be missing, never a gate the app waits on.

Merge `getgroupmy/get-ride#785` first: until `face-compare` is deployed without the gateway JWT check, the database's own calls to it are refused, whichever face server it points at.

The smallest useful first step needs no app change: point the Face match server key at this
service's `/compare`. A full integration would add a get-ride edge function that, after an
`identity_checks` upload, creates a verification here with the photos from storage, and store the
result on the check from the signed webhook. That is get-ride work, not done yet.

## Integrating (get-ride or any app)

**Hosted page (recommended, like other eKYC providers).** Your server creates a
verification with a `redirect_url`; the answer includes a one-time `hosted_url`.
Send the user there: get.ekyc's own page takes consent (English / Bahasa
Melayu), the document photos, the selfie and the head turns, then sends the user
back to `redirect_url?verification_id=…&status=…`. The decision itself reaches
your server by the signed webhook (never trust the return link). Your app needs
no camera screens, and capture improvements reach users without an app update.
Set `EKYC_PUBLIC_URL` so links use the public address. Full walk-through:
`/guide#hosted`.

**Or your own screens**, with the API calls below.

A step-by-step guide for app owners, with code in curl, Node/Deno, Python and Flutter, is served at
**`/guide`** (e.g. `https://ekyc.getride.my/guide`). Document types, reason codes and limits on it come
live from the running server. The summary below is the same flow.


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

## Start at boot (Mac mini)

Run once on the Mac mini (asks for your Mac password):

```bash
cd ~/get.ekyc && ./scripts/install_service.sh
```

The server and the Cloudflare tunnel (token read from `~/.cloudflared/ekyc.token`)
become startup services: they start at boot before anyone logs in, run as you,
and restart by themselves if they stop. The script also sets the Mac to restart
after a power failure and never to sleep. With FileVault on, a reboot waits at
the login screen for your password before anything starts.

- Restart by hand: `sudo launchctl kickstart -k system/my.getride.ekyc`
- Logs: `~/get.ekyc/server.log`, `~/.cloudflared/tunnel.log`
- Remove: `./scripts/install_service.sh --uninstall`

## Updating and the change log

Owners update from the console: **Changelog → Check for updates → Update now**.
It pulls the branch's upstream (fast-forward only; it refuses if the checkout
has local edits), installs requirements when `requirements.txt` changed,
fetches any new models, checks the new version imports, and restarts the
server in place with the same command line. If the check fails it rolls back
to the previous commit and keeps running. Every update is in the audit trail.

The Changelog tab lists every version the server has run, with the commits
each one brought in, who updated, and failed attempts. Updates made from the
terminal (`git pull` and a restart) are recorded at the next start too. The log
is `<data>/changelog.jsonl`.

Docker installs are not updated this way: rebuild the image instead.

## Fraud and compliance controls

| Control | What it does | Result |
|---|---|---|
| Reused photo | Perceptual hash of every upload; the same picture (resized or recompressed) under another user | review |
| Selfie copied from the ID | Selfie matches the ID portrait at ≥ 0.93, as only a photo of the photo does | review |
| Edited image | Photoshop, Snapseed, Canva… named in EXIF / XMP | review |
| Duplicate document / face | Same ID number or face under another user of the same app | review |
| Edited repeat attempt | Same ID number seen before with a different name or date of birth | review |
| Face blocklist | Admin "Block this person": that face is refused for every app | reject |
| Sanctions | UN Security Council list (downloaded daily) and an optional MOHA CSV; name in any order, birth year checked; the whole approved base is re-screened when the list changes | review |
| MyKad consistency | IC date / state code / gender digit vs LELAKI/PEREMPUAN, front vs back number, postcode vs state | review |
| Re-authentication | `POST /v1/verifications/reauth` (external_ref + selfie): is this the driver who was verified? For going online / random checks | match / no match |
| Consent | `consent: {version, accepted_at, text_sha256}` stored with the verification (PDPA s.40: explicit consent for biometrics) | recorded |
| Audit trail | Hash-chained: editing or deleting a row breaks every later hash; `GET /admin/api/audit/verify` checks it | tamper-evident |

Not yet built (from the research, in priority order): passive liveness model (MiniFASNet, Apache-2.0) on the
head-turn frames; screen-recapture / photocopy detection; a capture page that blocks gallery uploads and virtual
cameras (injection attacks); face-quality gating (BSI OFIQ, ISO 29794-5); NFC passport reading with passive
authentication (mobile app + BSI master list); Bank Negara FAR audit sampling and monthly reports (only if the
wallet is a licensed e-money product); the new-generation MyKad (issued since 17 Sep 2026) needs real samples.

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

## Measured speed

`scripts/benchmark.py` on synthetic documents, 23 Nov 2026:

| Check | Mac mini M6, 12 cores, 16 GB | Test machine, 4-core Xeon |
|---|---|---|
| Face compare (`/compare`) | 0.04 s · 1,368 a minute | 0.12 s · ~490 a minute |
| Passport + selfie | 0.27 s · 221 a minute | 0.82 s · ~73 a minute |
| Passport + selfie + 3 liveness frames | 0.34 s · 179 a minute | 1.19 s · ~50 a minute |
| MyKad + selfie | 0.82 s · 73 a minute | 2.33 s · ~26 a minute |

"A minute" is the service as shipped: one check at a time, each spread over all cores. With one
worker per core the Mac mini's ceiling is about 2,000 passport checks or 490 MyKad checks a minute,
but that needs a code change. The DS224+ has not been measured; expect it to be several times slower
than the Xeon because it has no AVX. To measure it, run the benchmark inside the container there.

## Measuring accuracy on real documents

Every test in this repository uses made-up documents. Before relying on the results, measure them
on real ones with `scripts/evaluate.py`. It runs each sample through the same checks as the service,
in its own process only: nothing is uploaded, written to the database, or sent anywhere.

1. **Collect 50–100 cases** from staff or drivers who have agreed in writing to their ID and photos
   being used for testing. Mix MyKads, passports and licences, and take the photos with phones as users
   would: some in poor light, some at an angle. Include cases that should fail: an expired document, a
   selfie of a different person, a photo of a photo.
2. **Lay them out** like `samples.example/`: one folder per case with `front.jpg`, `selfie.jpg`
   (plus `back.jpg` and `liveness_1..3.jpg` if you have them), and one row per case in `labels.csv`.
   `expected` is what an admin decided after looking at the case. `full_name`, `document_number`,
   `date_of_birth` and `expiry_date` are the true values from the document. Give each person one
   `person_id`.
3. **Run it** on the Mac mini, from a checkout:

```bash
EKYC_MODELS_DIR=models python scripts/evaluate.py samples --out eval-out
```

4. **Read `eval-out/report.md`**:
   - How often the engine wrongly approved or wrongly rejected a case.
   - How many cases it left for a person to decide.
   - Which reasons fire on genuine people.
   - How often each field was read correctly.
   - A face-threshold table of false rejects against false accepts.
   - The cases to look at first.

   `results.csv` has one row per case. Neither file contains names, numbers or dates unless you
   add `--show-values`.
5. **Delete the samples** when you are done. `samples/` and `eval-out/` are in `.gitignore` so they
   are never committed. Keep them off shared drives.

The script judges the engine's own verdict (as if `EKYC_AUTO_APPROVE=true`), so the numbers show how
much of an admin's work it could take over, whatever the setting in production. Re-run it after
changing a threshold. Small samples give rough signs, not rates, and the report says so.

## Face engine

- **Detection** (YuNet): when no face is found it retries on a contrast-corrected copy (dim photos)
  and an enlarged copy (small ID-card portraits); faces under 24 px are ignored; the best face is the
  largest weighted by detector confidence.
- **Matching** (SFace): each face is embedded together with its mirror image and averaged. The ID
  portrait is compared with the selfie *and* the straight-on liveness frame; the better live sample
  counts. Results carry `face_match` (cosine), `face_score` (0-100, the threshold shows as 80) and the
  ID portrait size in pixels.
- **Quality** (ISO 29794-5 style components): face size, sharpness, brightness, over-exposure, yaw,
  roll and pitch. A poor selfie goes to review with what to fix (`selfie_quality_low`) instead of
  becoming a false "no match".
- **Active liveness**: random head-turn order, every frame must be the same person.
- **Passive liveness (optional)**: MiniFASNetV2-SE anti-spoofing on the selfie and the straight-on
  frame (photos of screens and prints). A low margin sends the case to review
  (`EKYC_PASSIVE_LIVENESS_ACTION=reject` to reject). Enable it with:

```bash
python scripts/download_models.py models --with-antispoof
```

  **Licence note:** the model's code and weights are Apache-2.0
  ([suriAI/face-antispoof-onnx](https://github.com/suriAI/face-antispoof-onnx)), but the weights were
  trained on CelebA-Spoof, a dataset licensed for non-commercial research. Decide on that before using
  it commercially; a model trained on your own consented captures removes the question. In simulated
  tests it caught re-photographed screens in 3 of 4 cases and prints in 1 of 4 — a second opinion, not a
  certified (iBeta / ISO 30107-3) liveness check.

## What it learns from reviewers

Admin › Verifications › *What get.ekyc learned from your reviews* (`GET /admin/api/learning`): from
every case a person approved or rejected it reports the checks that sent genuine people to review,
how often the engine agreed with reviewers, the face-match scores of genuine people and impostors,
and **suggested** threshold changes. Nothing is applied automatically — thresholds are changed by an
admin (`.env`), after measuring with `scripts/evaluate.py`. It does not retrain models or change its
own code.

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
