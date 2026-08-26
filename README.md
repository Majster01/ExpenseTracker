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
categorization counts, rows added, and duplicate rows removed.

The backend also exposes a simple in-memory expense API:

```sh
curl http://127.0.0.1:8000/expenses
curl -X POST http://127.0.0.1:8000/expenses \
  -H 'Content-Type: application/json' \
  -d '{"title":"Groceries","amount":42.50,"category":"Food","date":"2026-08-26"}'
```

When `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are configured, the frontend
uses the Google Identity Services authorization-code flow. The backend exchanges
the one-time code, stores the encrypted per-user refresh token in Firestore, and
returns a short-lived access token while setting an HttpOnly application-session
cookie. The browser uses that cookie for `/statements`; Google refresh tokens are
never sent to or stored by the frontend.

The requested Google API scope is
`https://www.googleapis.com/auth/spreadsheets`. The `openid email profile` scopes
identify the Google subject used to key the Firestore token record. The user's
Google account must have access to the working spreadsheet. Set
`MAX_UPLOAD_BYTES` to change the 10 MiB upload limit.

`GOOGLE_CLIENT_ID` is required for the statements endpoint. The backend never
loads service-account credentials or Application Default Credentials; every
Sheets operation uses the authenticated caller's OAuth access token.

Uploaded PDFs are not written to disk by the endpoint. Google Sheets
deduplication prevents duplicate rows when a statement is uploaded again.

PDF extraction uses PyMuPDF and does not require `pdftotext` or another external
PDF executable.

## Deploy the application to Cloud Run

The repository-root container serves both the API and the static PWA. Cloud Run
supplies the `PORT` environment variable, and the container binds to
`0.0.0.0`. The frontend calls `/auth/...` and `/statements` on the same origin.

Do not put service-account keys in the container or Cloud Build settings. The
backend uses the caller's OAuth credentials and requires each caller to have
access to the target spreadsheet. Store `GOOGLE_CLIENT_SECRET` and
`TOKEN_ENCRYPTION_KEY` in Secret Manager and grant the Cloud Run runtime service
account access to those secrets and the Firestore database.

Enable these APIs in the Google Cloud project:

```sh
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com containeranalysis.googleapis.com \
  iam.googleapis.com sheets.googleapis.com secretmanager.googleapis.com \
  developerconnect.googleapis.com
```

Create the Artifact Registry repository once in the same region as Cloud Run:

```sh
gcloud artifacts repositories create expense-tracker \
  --repository-format=docker --location=europe-west1
```

Connect the GitHub repository in Cloud Build or Developer Connect and create a
trigger for `master` using `cloudbuild.yaml`. The trigger builds one image,
which contains `backend/` and `frontend/`, and deploys one Cloud Run service.
Set `_REGION`, `_SERVICE`, `_REPOSITORY`, and `_IMAGE` substitutions if the
defaults do not match your project. Set `_ALLOWED_ORIGINS` and
`_GOOGLE_REDIRECT_URI` to the exact final frontend origin, without a trailing
slash. A custom domain is preferred; the generated Cloud Run service URL also
works.

For a GitHub trigger created from the CLI, the shape is:

```sh
gcloud builds triggers create github \
  --name=expense-tracker-deploy \
  --repo-owner=YOUR_GITHUB_OWNER \
  --repo-name=YOUR_REPOSITORY \
  --branch-pattern='^master$' \
  --build-config=cloudbuild.yaml \
  --substitutions=_REGION=europe-west1,_SERVICE=expense-tracker-api,_REPOSITORY=expense-tracker,_IMAGE=api,_GOOGLE_CLIENT_ID=YOUR_CLIENT_ID,_ALLOWED_ORIGINS=https://YOUR_FRONTEND_ORIGIN,_GOOGLE_REDIRECT_URI=https://YOUR_FRONTEND_ORIGIN
```

Grant the Cloud Build service account permission to push to Artifact Registry
and deploy Cloud Run services. Grant the Cloud Run runtime service account
access to the two secrets and Firestore. Configure the Google OAuth web client
with the same frontend origin as an authorized JavaScript origin and redirect
URI.

The supplied Cloud Build configuration allows unauthenticated network access
to Cloud Run while the application enforces Google OAuth when
`GOOGLE_CLIENT_ID` is configured. Keep `ALLOWED_ORIGINS` restricted to the
actual frontend origin before exposing personal data. Credentialed browser
requests require the exact origin and the session cookie uses `Secure`,
`HttpOnly`, and `SameSite=Lax`. Same-origin serving removes the third-party
cookie dependency that affected the GitHub Pages deployment.

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
`frontend/` contains the static PWA served by the same Cloud Run service. Set
the `googleClientId` value in `frontend/config.js` to the Web OAuth client ID
before deploying. `apiBaseUrl` is intentionally empty so the browser uses the
same origin; the spreadsheet link is configured in the same file.
Runtime data and generated CSV files remain at the project root.

## Legacy GitHub Pages deployment

GitHub Pages can still host an older frontend copy, but it keeps the
cross-origin cookie and CORS dependency. Use the Cloud Run URL or custom domain
for the mobile PWA after enabling the combined-service trigger.

Create a Google OAuth Web client and add the final Cloud Run/custom-domain
origin to its authorized JavaScript origins. Configure the OAuth consent screen
and enable the Google Sheets API. Set the same client ID in
`frontend/config.js` and the Cloud Run `_GOOGLE_CLIENT_ID` substitution.
`_ALLOWED_ORIGINS` and `_GOOGLE_REDIRECT_URI` must use that same origin. For the
popup authorization-code flow, the backend exchanges the code using that exact
origin as `redirect_uri`.

When `GOOGLE_CLIENT_ID` is configured, the API requires a valid Google OAuth
access token in the upload request. CORS is restricted to `ALLOWED_ORIGINS`; do
not use `*`. The account must be able to access the configured working sheet.
The app currently supports PDF uploads with the `nlb` and `otp` parsers. CSV
uploads are intentionally deferred.

The login and refresh endpoints return short-lived access tokens for the
frontend session manager, while the durable refresh token remains encrypted in
Firestore. Sign out invalidates the application session and clears its cookie;
revoking Google consent is a separate operation. Only load trusted scripts on
the frontend origin because same-origin scripts can initiate authenticated
requests.
