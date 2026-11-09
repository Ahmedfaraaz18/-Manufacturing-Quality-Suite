# Manufacturing Quality Suite

Two Django + DRF + PostgreSQL applications in one project:

- **Round 2 - AI Visual Quality Inspection** (`inspections/`): engineers upload JPG/PNG images of the fictional product `P-PLATE01`; the supplied classifier (`BIT-PLATE-CV-1.0`) labels each one Normal or Defective in the background (Celery + Redis); supervisors confirm or correct predictions; administrators register products.
- **Round 1 - Manufacturing Manual Assistant** (`assistant/`): machine-scoped RAG over PDF manuals with verified citations. Unchanged; still available under "Manual assistant" in the sidebar.

> Educational demonstration with synthetic images. It is **not** a real factory inspection system, and model scores are uncalibrated.

## Round 2 quick start (Windows PowerShell, from this folder)

```powershell
# 0. Start the required local services; credentials are read from .env
docker start hackathon-postgres hackathon-redis
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo --password "<choose a password>"   # creates only missing admin_demo / engineer_demo / supervisor_demo
.\.venv\Scripts\python.exe manage.py import_products                              # imports P-PLATE01 from product_details.csv (idempotent)
.\.venv\Scripts\python.exe manage.py import_round2_data                           # registers the 60 samples + 3 validation files (no live inspections)
.\.venv\Scripts\python.exe manage.py model_smoke_test                             # checksums + one real prediction

# Terminal 1 - web server (use 0.0.0.0:8000 for LAN demos, see "Temporary Wi-Fi demo" below)
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000

# Terminal 2 - Celery worker (REQUIRED for inspections to leave the Queued state; --pool=solo is the reliable pool on Windows)
.\.venv\Scripts\celery.exe -A config worker --pool=solo --loglevel=INFO
```

Open <http://127.0.0.1:8000/> and sign in. Each role lands on its own workspace.

### Demo-account credentials

Demo usernames are `admin_demo`, `engineer_demo`, and `supervisor_demo`. Their password is supplied only by the authorized operator, either through `DEMO_USER_PASSWORD` in the local environment or the `seed_demo --password` argument; it is never stored in this repository or printed by the command.

`seed_demo` is safe to re-run: it creates only missing accounts and preserves existing passwords, roles, and privileges. To intentionally replace a known demo password, use `seed_demo --reset-password --password "<new password>"`. To intentionally restore the documented roles as well, add `--sync-roles`. Do not use either reset option unless you are authorized to change those credentials.

Other commands:

```powershell
.\.venv\Scripts\python.exe manage.py evaluate_model        # real classifier on the 60 labelled samples -> evaluation_reports\
.\.venv\Scripts\python.exe manage.py requeue_inspections   # recover items saved but never queued (after a crash/outage); safe to repeat
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test                  # Round 1 + Round 2 (isolated test database)
```

`requirements.txt` lists the direct dependencies at the versions this project was built and tested with.

### Roles (enforced on the server)

| Role | Can | Cannot |
|---|---|---|
| Administrator | Register/edit/deactivate products; view all inspections and statistics | Upload images, review |
| Engineer | Upload single images and batches (1-10); see **only their own** inspections and batches | Review, see other engineers' records, manage products |
| Supervisor | See all inspections; filter; confirm or correct **completed** inspections with an optional comment | Upload, manage products |

An Engineer who calls `POST /api/inspections/{id}/review/` or opens/posts `/inspections/{id}/review/` receives **HTTP 403** before the inspection is even looked up; neither the prediction nor any review changes.

### Pages and API

| Page | URL |
|---|---|
| Engineer: new inspection (single + batch) | `/inspections/upload/` |
| Engineer: my history | `/inspections/history/` |
| Supervisor: review queue (filters, review form on the record page) | `/inspections/review-queue/` |
| Administrator: products, statistics, model info | `/inspections/manage/` |
| Administrator: synthetic evaluation scorecard | `/inspections/evaluation/` |
| Inspection record / batch status | `/inspections/<id>/`, `/inspections/batches/<id>/` |

| Endpoint | Who | Purpose |
|---|---|---|
| `GET/POST /api/products/`, `PATCH /api/products/<id>/` | read: any role, write: Administrator | Products |
| `POST /api/inspections/upload/` (field `image`) | Engineer | One image -> 202 (queued) or 422 (rejected, failure record saved) |
| `POST /api/inspections/batch/` (repeated field `files`, 1-10) | Engineer | Batch -> 202; 400 for empty or >10 files |
| `GET /api/inspections/` (`q`, `predicted_class`, `status`, `reviewed`, `date_from`, `date_to`, `product`, `batch`) | any role, scoped by role | Paginated history |
| `GET /api/inspections/<id>/`, `GET /api/batches/<id>/` | any role, scoped by role | Detail / live status |
| `GET /api/supervisor/inspections/` | Supervisor | Same filters over all records |
| `POST /api/inspections/<id>/review/` (`reviewed_class`, `comment`) | Supervisor | 201; 409 if duplicate or not completed |

### How it works

1. **Validation first.** Each file is decoded with Pillow (not trusted by name or MIME type): JPEG/PNG only, <= 5 MiB, 64-4096 px per side (checked from the header before decoding, so decompression bombs are refused), no animation, full decode to catch truncation. A rejected file becomes a persisted `FAILED` record (reason, user, product, timestamp, **no image kept, no prediction**) and is never sent to the classifier. One bad file never stops the others.
2. **Queue.** Valid files are stored under a generated name and a Celery task is published *after the database commit*. If Redis is unreachable the items are marked `FAILED` with a readable reason instead of hanging; `requeue_inspections` recovers items left `QUEUED` by a crash.
3. **Worker.** `QUEUED -> PROCESSING -> COMPLETED/FAILED`, persisted at each step. The task imports the supplied `inference.py` unchanged and calls its `predict` (the helper reads the 1.4 KB weights file on each call; the imported module and its checksum verification are cached per process). The result is written with a conditional update, so a retry or duplicate delivery can never overwrite a stored prediction. Only database outages are retried.
4. **Immutability.** The original `predicted_class`, `confidence`, `defect_probability`, `model_version` of a completed inspection are guarded in the model and by database `CHECK` constraints. A supervisor's decision lives in a separate `InspectionReview` row (one per inspection, `PROTECT` foreign keys), and both are always shown.
5. **Duplicate submissions.** The browser disables the form while uploading and sends a one-time token; a repeated send with the same token returns the existing batch instead of creating another.

### Model evaluation (60 labelled samples, real classifier)

`python manage.py evaluate_model` classifies every image first and reads `labels.csv` only afterwards; the three files in `Validation/` are excluded. Report files: `evaluation_reports/round2_evaluation.json` and `round2_predictions.csv`. The Administration **Model evaluation** page presents the latest saved report. Run `python manage.py import_round2_data` after migrations to inventory the 60 sample files (hash, dimensions, validation status) and the three deliberately invalid validation files; these records are distinct from Engineer inspections.

| | Predicted Normal | Predicted Defective |
|---|---:|---:|
| **Actual Normal** | TN = 30 | FP = 0 |
| **Actual Defective** | FN = 0 | TP = 30 |

60 samples, 60 successful predictions, 0 failed. **Defect recall = TP / (TP + FN) = 30 / 30 = 1.00**, accuracy 1.00.

There were no false negatives, but that does **not** show the model is reliable: 60 synthetic, fixed-framing images from the same generator as the training data are development data, not a blind benchmark. Real photographs, rotated or displaced products, different backgrounds and unfamiliar defects are outside the model's validated scope. The model only says Normal/Defective; it does not locate or grade defects. The same predictions were obtained under the pack's pinned runtime (numpy 2.3.5, Pillow 12.3.0) and the installed one (numpy 2.5.4, Pillow 13.0.0): 0 class differences, maximum probability difference 0.0.

### Evaluator demonstration checklist

1. Sign in as **admin_demo** -> *Products & overview*: show P-PLATE01 (register/edit with the modal), statistics, classifier card.
2. Sign in as **engineer_demo** -> *New inspection*: choose `Samples\images\inspection_002.jpg`, run; show the image, class, confidence, defect probability, model version, timestamp.
3. *Batch* tab: choose several sample images **plus** the three files from `Validation\`. The panel shows per-item status; failed items show readable reasons while valid ones complete.
4. To *show* Queued -> Processing -> Completed live, stop the Celery worker (Ctrl+C), submit a batch (items stay **Queued** in Redis and the database), then start the worker again.
5. Sign in as **supervisor_demo** -> *Review queue*: filter (prediction, status, reviewed, dates, search), open a completed record, **Confirm** or **Correct** with a comment, reload: original prediction and review are both shown.
6. As **engineer_demo**, open `/inspections/<id>/review/` -> **403 Access denied**; the browser console snippet below shows the API denial.
7. Show `evaluation_reports/round2_evaluation.json` or rerun `evaluate_model`; explain recall and the limitations above.

```javascript
// Engineer session, browser console: server-side denial of a review attempt
fetch('/api/inspections/1/review/', {method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.cookie.match(/csrftoken=([^;]+)/)[1]},
  body: JSON.stringify({reviewed_class: 'Normal', role: 'supervisor'})}).then(r => r.status)   // -> 403
```

### Testing

`manage.py test` runs 200+ tests in an isolated PostgreSQL test database. Classifier-mocked tests (validation, upload API, permissions, processing, pages) are separate from `inspections/tests/test_real_model.py`, which runs the **real** supplied classifier (60-sample evaluation, supplied invalid files, end-to-end pipeline). The true Redis + Celery worker round trip is verified by running a worker (see quick start) rather than in the unit-test suite.

### Known limitations

- Windows: the Celery worker must use `--pool=solo` (or `threads`); the default prefork pool is not supported.
- A very fast worker finishes items in milliseconds, so Queued/Processing may not be visible unless the worker is paused (see checklist step 4).
- `predict()` in the supplied helper re-reads the small weights file per call; it was left unmodified on purpose.
- Reviews are final (one per inspection). Failed items cannot be reprocessed from the UI; resubmit the image.
- Uploaded images are kept on local disk under `media/` (not publicly served; delivered only through authenticated, role-scoped URLs).
- The development server is not suitable for production. See the LAN notes below.

## Round 3: Quality Incident and Action Assistant

The Round 3 workflow is part of the existing `inspections` application. It creates one incident
from an Engineer's own completed inspection only when the immutable original classifier output is
`Defective`; a separate Supervisor correction does not change eligibility. Repeated and concurrent
requests return the same database-unique incident without replacing saved actions or review data.

The dedicated quality-SOP service reads `product_sop_mapping.csv` and the PDFs in `SOPs/` (or
`round3_pack/`). The inspected organizer mapping specifies `Q-PLATE-01` revision `1.0` and
`Q-PLATE-02` revision `1.0` as required for `P-PLATE01`. The unrelated `P-GASKET02` SOP and the
Round 1 equipment-manual index are excluded. Mapped PDF identity and revision are checked before
retrieval; source links serve only mapped PDFs to authenticated users and open at the cited page.
If required sources or citations cannot be verified, the incident remains saved as **Insufficient
evidence**, with no suggested actions and a manual-review reason.

Engineers open an inspection from **My inspections**, then use **Create Quality Incident** or
**View existing incident**. Supervisors use **Quality incidents** to inspect citations and submit a
commented decision. Administrators may view incidents but cannot submit Supervisor decisions.
Review status is stored separately from evidence status, and an insufficient-evidence incident
cannot be approved for follow-up.

| Page / endpoint | Purpose |
|---|---|
| `/quality-incidents/` | Role-scoped incident workspace |
| `/quality-incidents/<id>/` | Persisted incident, actions, citations, and review |
| `POST /inspections/<id>/quality-incident/` | Engineer HTML incident creation |
| `POST /api/inspections/<id>/incident/` | Engineer API incident creation |
| `GET /api/quality-incidents/` and `/api/quality-incidents/<id>/` | Role-scoped incident API |
| `POST /api/quality-incidents/<id>/review/` | Supervisor-only decision with mandatory comment |

Run the focused Round 3 tests with:

```powershell
.\.venv\Scripts\python.exe manage.py test inspections.tests.test_quality_incidents
```

The checked-in SOPs are fictional challenge material and are not operating instructions for a real factory.

---

# Round 1: Manufacturing Manual Assistant

Django + DRF + PostgreSQL RAG app: engineers pick a machine, ask a maintenance question and get an answer grounded in that machine's PDF manuals, with document ID, revision, PDF page and a link to the source page. If the evidence is missing, the app refuses.

## Run

```powershell
# Postgres 16 and Redis containers must be running; credentials come from .env (see .env.example)
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo --password "<choose a password>"   # creates only missing demo users
.\.venv\Scripts\python.exe manage.py ingest_dataset --csv "<pack>\machine_manual_index.csv" --manuals-dir "<pack>\Manuals"
.\.venv\Scripts\python.exe manage.py runserver
.\.venv\Scripts\python.exe manage.py test assistant
```

Manuals can also be registered and uploaded through the Administrator pages (`/manage/`). Indexing is synchronous by default; set `INGEST_ASYNC=True` and run `celery -A config worker` to use Celery.

## Temporary Wi-Fi demo

For a trusted private network, include the laptop's Wi-Fi IPv4 address alongside `localhost` and
`127.0.0.1` in `.env` as `DJANGO_ALLOWED_HOSTS`, then run:

```powershell
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:8000
```

Allow TCP port 8000 only on the Private firewall profile, restricted to the local subnet. Do not
forward this port through the router. This makes the whole Django site, including the authenticated
Admin login, reachable on that network; a firewall port rule cannot hide a specific URL. Django's
development server is not suitable for a public or production deployment.

## Layout (`assistant/`)

- `models.py`, `serializers.py`, `permissions.py`, `urls.py`, `views/api.py` (DRF), `views/pages.py` (templates)
- `services/ingestion.py` (validation, PyMuPDF, indexing), `chunking.py`, `embeddings.py` (sentence-transformers), `retrieval.py`, `llm.py` (Groq), `rag.py` (orchestration + citation validation)
- `tests/`, `templates/assistant/`, `static/assistant/`

## Behaviour worth knowing

- **Machine isolation:** `retrieval.retrieve` filters `DocumentChunk` by the selected machine in SQL before any similarity is computed.
- **Embedding startup:** the model loads lazily on the first embedding request and is reused within that worker. The first request can be slow. `EMBEDDING_WARMUP_ENABLED=True` opts into synchronous startup loading; it is disabled by default.
- **Groq latency:** requests default to a 5-second timeout and no automatic SDK retries (`GROQ_TIMEOUT_SECONDS` and `GROQ_MAX_RETRIES` are configurable). A compatibility retry for unsupported reasoning options uses the remaining time. A timeout is reported as a service error, not an answer; network read timeouts are not a strict end-to-end deadline.
- **Citations:** the LLM returns claims with evidence IDs (`S1`, ...) and exact quotations. A claim is discarded if any evidence ID or quotation is invalid. The answer displays verified quotations rather than model-written claims, with document/revision/page built from stored chunks. If none remain, the response is "insufficient evidence". Exact quotation verifies provenance, not that a passage actually answers the question or that its interpretation is correct; users should check the linked manual.
- **History:** answers and source snapshots are still saved. History displays only quotations that can still be checked against their indexed chunks; older model-written answers and unavailable or unverifiable passages are not displayed.
- **Roles (server-side):** Administrator manages machines/manuals; Engineer asks questions and sees own history; Supervisor has read-only history for everyone. Any signed-in user may open manual PDFs; anonymous users cannot.
- **Scope:** only the three supplied manuals are indexed. The CSV is registration metadata only.
