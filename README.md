# ExpenseTracker

## HTTP backend

Install dependencies with `.venv/bin/python -m pip install -r backend/requirements.txt`,
then start the private API:

```sh
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
# or: ./run_api.sh
```

Upload a statement with the parser type as multipart form data:

```sh
curl -F 'parser_type=nlb' -F 'file=@/path/to/statement.pdf' \
	http://127.0.0.1:8000/statements
```

The endpoint parses the uploaded PDF in memory and sends rows directly to Google
Sheets. The response contains the parser type, statement ID, parsed row counts,
categorization counts, rows added, and duplicate rows removed. The endpoint has no `/login` route and no
application-level authentication. Run it only on a private or trusted
network; do not expose it directly to the public internet.

The backend also exposes a simple in-memory expense API:

```sh
curl http://127.0.0.1:8000/expenses
curl -X POST http://127.0.0.1:8000/expenses \
  -H 'Content-Type: application/json' \
  -d '{"title":"Groceries","amount":42.50,"category":"Food","date":"2026-08-26"}'
```

The service account is loaded by the backend and is never accepted from the
mobile client. Set `SERVICE_ACCOUNT_FILE` to use a different credential path
and `MAX_UPLOAD_BYTES` to change the 10 MiB upload limit.

Uploaded PDFs are not written to disk by the endpoint. Google Sheets
deduplication prevents duplicate rows when a statement is uploaded again.

PDF extraction uses PyMuPDF and does not require `pdftotext` or another external
PDF executable.

## Deploy the backend to Cloud Run

The backend is containerized from the repository root. Cloud Run supplies the
`PORT` environment variable, and the container binds to `0.0.0.0`.

Before deploying, rotate the key represented by `service_account.json`: it has
been committed to Git history. Remove the file from the repository and remote
history after rotation. Do not put it in the container or Cloud Build settings.

The recommended production setup is a dedicated Cloud Run runtime service
account. Grant it access to the Sheets API and share the target spreadsheet
with its email address. When `SERVICE_ACCOUNT_FILE` is unset, the API uses
Google Application Default Credentials, which is the mode used on Cloud Run.
For local development, set `SERVICE_ACCOUNT_FILE` to a local JSON key path.

Enable these APIs in the Google Cloud project:

```sh
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com containeranalysis.googleapis.com \
  iam.googleapis.com sheets.googleapis.com secretmanager.googleapis.com \
  developerconnect.googleapis.com
```

Create the Artifact Registry repository once:

```sh
gcloud artifacts repositories create expense-tracker \
  --repository-format=docker --location=us-central1
```

Connect the GitHub repository in Cloud Build or Developer Connect and create a
trigger for the deployment branch using `cloudbuild.yaml`. Set `_REGION`,
`_SERVICE`, `_REPOSITORY`, and `_IMAGE` substitutions if the defaults do not
match your project. Grant the Cloud Build service account permission to push
to Artifact Registry and deploy Cloud Run services.

The supplied Cloud Build configuration allows unauthenticated access for
initial testing. This API has no application-level authentication, so do not
use that setting for production. Remove `--allow-unauthenticated` and grant
Cloud Run Invoker only to the intended caller before exposing personal data.

The `/expenses` endpoints remain in-memory and can reset when Cloud Run
restarts or scales. Statement imports are persisted in Google Sheets.

Test the image locally:

```sh
docker build -t expense-tracker-api .
docker run --rm -p 8080:8080 expense-tracker-api
curl http://localhost:8080/docs
```

## Project layout

`backend/` contains the API entry point, processor, and bank parser modules.
`frontend/` is reserved for the future mobile client and is intentionally empty.
Runtime data and generated CSV files remain at the project root.