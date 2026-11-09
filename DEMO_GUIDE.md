# Manufacturing Quality Suite: live demonstration guide

This guide uses the supplied model and synthetic organiser data only. It does not claim real-world manufacturing performance.

## Start-up

From the `hackathon` directory, open three PowerShell terminals:

```powershell
# Terminal 1: dependencies
docker start hackathon-postgres hackathon-redis

# Terminal 2: background inspection worker (Windows-safe pool)
.\.venv\Scripts\celery.exe -A config worker --pool=solo --loglevel=INFO

# Terminal 3: web application
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000 --noreload
```

Before the demo, run the safe, read-only checks below. Database setup commands are documented in the README and should only be run by the authorised operator.

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py model_smoke_test
```

Use the existing demo accounts (`admin_demo`, `engineer_demo`, and `supervisor_demo`). Their password is set locally by the authorised operator and is deliberately not recorded here.

## Demonstration sequence

1. Sign in as the administrator. Open **Products & overview**, show P-PLATE01, the compact model/integrity card, and **Model evaluation**. Explain that the 60-sample result is synthetic-only.
2. Sign in as the engineer. Open **New inspection**, select P-PLATE01, upload `Samples\images\inspection_002.jpg`, and open the resulting record when it completes.
3. Show the original model class, confidence, defect probability, model version, and the visible image. Download the PDF report and confirm the image, metadata, and disclaimer are present.
4. Select the **Batch** tab and submit two sample images plus one file from `Validation\`. Show that valid images complete while the invalid file is recorded as failed without reaching the classifier.
5. Sign in as the supervisor. Open **Review queue**, filter if useful, open the completed inspection, then confirm or correct it with a comment. Reload the page to show the original AI result remains separate from the final review.
6. Open the Manual Assistant as the relevant role, select a registered manual/machine, ask a question grounded in that manual, and show the source citation and page reference.

## Backup plan

- If a new inspection remains queued, check that the worker terminal is running and use `manage.py requeue_inspections` only after confirming the worker and Redis are available.
- If Redis is intentionally stopped, uploads are recorded as failed with a clear reason rather than marked completed. Restart Redis and the worker before retrying with a new upload.
- Keep `Samples\images\inspection_001.png` and `inspection_002.jpg` ready locally. Never use organiser labels in the live inspection flow.
- If a document/LLM service is unavailable, demonstrate saved manuals, retrieval scope, and existing question history rather than claiming a live answer succeeded.

## Judge Q&A anchors

- **Why separate confidence and defect probability?** The classifier uses defect probability for its 0.50 decision rule; confidence is the score for the selected class. Both are uncalibrated model scores.
- **Can a supervisor overwrite the model?** No. A review is a separate immutable audit record; the original output remains visible.
- **How are uploads protected?** JPEG/PNG content is decoded and bounded before inference; uploaded media is delivered through authenticated, role-scoped views.
- **What happens if the worker is unavailable?** The persisted record is failed with a clear reason, and recovery tooling can safely requeue suitable records.
