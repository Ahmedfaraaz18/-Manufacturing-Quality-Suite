# Manufacturing Manual Assistant

Django + DRF + PostgreSQL RAG app: engineers pick a machine, ask a maintenance question and get an answer grounded in that machine's PDF manuals, with document ID, revision, PDF page and a link to the source page. If the evidence is missing, the app refuses.

## Run

```powershell
# Postgres 16 and Redis containers must be running; credentials come from .env (see .env.example)
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_demo --password "<choose a password>"   # admin_demo / engineer_demo / supervisor_demo
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
