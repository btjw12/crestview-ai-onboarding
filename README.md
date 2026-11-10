# Crestview Capital Group: AI-Powered Client Onboarding

A proof of concept that reads client onboarding applications from a monday.com board, runs them
through a two-stage LLM pipeline (compliance risk triage, then onboarding summary), routes each
application to a review type, and writes validated, structured results back to the same
monday.com items.

It can run as a batch CLI (`main.py`) or event-driven through a webhook server
(`webhook_server.py`) that processes an item when its **AI Processing Status** is set to
`Ready for AI Review`. Result JSON can optionally be archived to Google Drive.

> All client data in this repo and on the demo board is fictional.

```
monday.com board ──(GraphQL read, paginated)──► ClientApplication
                                                    │
                                   Stage 1: RiskAssessor  ──► RiskAssessment (validated)
                                                    │            + review-routing policy
                                                    │              (review type, human review)
                                   Stage 2: SummaryGenerator ──► OnboardingSummary (validated)
                                                    │
                                              OnboardingResult ──► output/*.json
                                                    │
monday.com item ◄──(change_multiple_column_values)──┘   (skipped with --dry-run)
   + Screening Status set to "Not Started" only if empty
                                                    │
                                   Google Drive folder ◄── output JSON   (only with --archive-drive)
```

Event-driven mode:

```
monday.com status change ──► POST /webhook/monday/<secret> (webhook_server.py)
   AI Processing Status = "Ready for AI Review"      │
                                                     ├─ claim item in SQLite (webhook_events.db)
                                                     ├─ AI Processing Status → "Processing"
                                                     ├─ run: main.py --item-id <id> --archive-drive
                                                     └─ AI Processing Status → "Completed" / "Failed"
```

## Installation

Requires Python 3.11+.

```bash
cd crestview-ai-onboarding
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then edit .env
```

`requirements.txt` covers the CLI and tests. The webhook server and Google Drive archiving need
extra packages that are not listed there yet:

```bash
pip install fastapi uvicorn google-api-python-client google-auth-oauthlib google-auth-httplib2
```

Note that `main.py` imports `google_drive` at startup, so the Google packages are currently
needed even if you never use `--archive-drive`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `MONDAY_API_TOKEN` | yes | monday.com personal API token (Profile → Developers → My access tokens) |
| `ANTHROPIC_API_KEY` | yes (not for `--check-connection`) | Anthropic (Claude) API key |
| `MONDAY_BOARD_ID` | no | Defaults to `5032323173` |
| `MONDAY_API_VERSION` | no | Pin a monday.com API version, e.g. `2025-04`. Blank uses the account default |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-opus-5-5`. Any current Claude model ID works (e.g. `claude-sonnet-5-5`, `claude-haiku-5-5` for lower cost) |
| `LOG_LEVEL` | no | `INFO` (default) or `DEBUG` |
| `GOOGLE_DRIVE_FOLDER_ID` | only for `--archive-drive` | ID of the Google Drive folder that receives the result JSON |
| `MONDAY_WEBHOOK_SECRET` | only for `webhook_server.py` | Random secret that forms the last segment of the webhook URL. The server refuses to start without it |

`.env` is git-ignored. Blank values and placeholders starting with `your-` count as "not set".
Keep real credentials in `.env` only. `.env.example` is committed and must contain placeholders.

## Running

```bash
# 1. Check monday.com connectivity and column mapping (no AI calls, no writes)
python main.py --check-connection

# 2. Generate results without modifying monday.com
python main.py --dry-run --limit 3

# 3. Process a single application by its Application ID
python main.py --application-id APP-2026-0303 --dry-run

# 4. Full run: process every application and write results back
python main.py

# 5. Process a single monday.com item by its item ID (this is what the webhook server runs)
python main.py --item-id 1234567890 --dry-run

# 6. Process and archive the result JSON to Google Drive
python main.py --limit 3 --archive-drive

# Offline demo with the bundled fictional sample data (always dry-run, needs only ANTHROPIC_API_KEY)
python main.py --sample-file sample_data/applications.json
```

| Flag | Purpose |
|---|---|
| `--dry-run` | Run the full AI pipeline and save JSON, but never call the monday.com mutation |
| `--limit N` | Process only the first N items. Pagination stops as soon as N items are fetched |
| `--application-id ID` | Process only the application whose Application ID matches `ID`. All items are read, then filtered, so `--limit` is ignored. Exits with code `1` if the ID is not found |
| `--item-id ID` | Process only the monday.com item with this item ID. All items are read, then filtered, so `--limit` is ignored. Exits with code `1` if the item is not found |
| `--archive-drive` | After saving the local JSON, upload it to the Google Drive folder in `GOOGLE_DRIVE_FOLDER_ID`. Works with `--dry-run` too (dry-run only skips the monday.com write-back) |
| `--check-connection` | Print the board's column mapping and the first few items (5, or `--limit`), then exit |
| `--sample-file PATH` | Read applications from local JSON instead of monday.com (forces `--dry-run`) |
| `--output-dir DIR` | Where result JSON is written (default `output/`) |
| `-v, --verbose` | Debug logging |

Each run prints a results table (client, risk, complexity, review, status, written back) and
writes `output/onboarding_results_<timestamp>.json`. The file has run metadata (`run_at`,
`board_id`, `source`, `dry_run`, `model`, `processed`, `succeeded`, `failed`) and one entry per
item with its status, `failed_stage`, error message, `written_back` flag and the full structured
result.

Exit codes: `0` all succeeded, `1` at least one item failed, monday.com could not be read,
`--application-id`/`--item-id` was not found, no applications were loaded, or the Google Drive
archive failed, `2` configuration error.

### Google Drive archiving

`google_drive.py` uploads the result JSON with the Drive API, using OAuth with the narrow
`drive.file` scope (the app can only see files it created).

1. In Google Cloud Console, enable the Drive API and create an OAuth client ID of type
   *Desktop app*. Download it as `credential_google.json` in the project root.
2. Set `GOOGLE_DRIVE_FOLDER_ID` in `.env` (the ID at the end of the folder's URL).
3. Run `python google_drive.py` once. A browser window opens for consent and the token is cached
   in `token.json` (mode `600`). It prints the connected account.
4. Optionally run `python test_drive_upload.py` to upload a small test file. It uses a hard-coded
   folder ID, not `GOOGLE_DRIVE_FOLDER_ID`, and creates a real file.

`credential_google.json` and `token.json` are git-ignored. If archiving fails, the error is
logged, the local JSON is kept and the run exits with code `1`.

### Webhook server (event-driven processing)

`webhook_server.py` is a FastAPI app that processes one item at a time when compliance moves it
to `Ready for AI Review`.

```bash
uvicorn webhook_server:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health        # {"status": "healthy"}
```

It needs `MONDAY_API_TOKEN` and `MONDAY_WEBHOOK_SECRET`. Because each run uses `--archive-drive`,
it also needs `ANTHROPIC_API_KEY`, `GOOGLE_DRIVE_FOLDER_ID` and a valid `token.json`.

**monday.com setup.** The board needs an **AI Processing Status** status column (ID
`color_mm8y4j3s`) with the labels `Ready for AI Review`, `Processing`, `Completed` and `Failed`.
Expose the server on a public HTTPS URL (for example with ngrok during a demo) and add a webhook
on the board for status column changes, pointing to:

```
https://<your-host>/webhook/monday/<MONDAY_WEBHOOK_SECRET>
```

The server answers monday.com's `challenge` handshake automatically.

**Request handling** (`POST /webhook/monday/{secret}`):

1. The path secret is compared in constant time with `MONDAY_WEBHOOK_SECRET` (`401` if wrong).
2. Events are ignored (HTTP 200 with `"status": "ignored"` and a reason) unless the event type is
   `update_column_value` or `change_status_column_value`, the board is `MONDAY_BOARD_ID`, the
   column is AI Processing Status and the new label is `Ready for AI Review`.
3. The item is claimed in SQLite (`webhook_events.db`, table `webhook_jobs`) with an atomic
   `INSERT OR IGNORE`, so monday.com retries and double clicks never start a second run.
4. A background task sets the status to `Processing`, runs
   `main.py --item-id <id> --archive-drive` in a subprocess (live mode, with write-back), then sets
   `Completed` and records `completed`. Any non-zero exit sets `Failed` and records `failed`.

**Duplicates.** If an item was already claimed, the webhook returns `"status": "duplicate"` and
does not re-run it. For a previously completed item the board status is put back to `Completed`.
Failed or in-progress items are left alone. To re-process an item, delete its row from
`webhook_jobs`.

### Resetting the board

`reset_board.py` clears the AI output columns on every item so the pipeline can be re-run from a
clean state. It runs as a preview by default and only changes the board with `--execute`:

```bash
python reset_board.py            # preview: list the items that would be cleared
python reset_board.py --execute  # clear the AI output columns on every item
```

It clears only the columns in `OUTPUT_COLUMNS` (see the write-back table below). It does **not**
touch the input columns, the Screening Status column or the AI Processing Status column, and it
does not clear the webhook's SQLite records.

## How the GraphQL integration works

`src/monday_client.py` makes all calls to `https://api.monday.com/v2`.

1. **Column discovery.** `boards(ids:) { columns { id title type } }` fetches the board schema.
   Input columns are **not hardcoded**: `map_input_columns` matches column titles (normalised)
   against aliases for `application_id`, `client_type`, `requested_services`, `estimated_aum` and
   `description`. Exact title matches win over partial ones. The AI output columns and the
   Screening Status and AI Processing Status columns are excluded, so they can never be read as
   input. The mapping is logged
   at startup, and unmatched fields are logged as warnings and treated as missing information.
2. **Reading items.** `boards { items_page(limit:) { cursor items {...} } }` returns the first
   page (up to 100 items), then `next_items_page(cursor:)` is called until the cursor is `null`
   (or `--limit` is reached). The `text` of each column value is used, so status, dropdown and
   number columns all map to readable strings. The item name becomes `client_name`, and the item
   `id` is kept for write-back.
3. **Writing back.** For each item, the client first reads the item's current column values, then
   sends a single `change_multiple_column_values(board_id, item_id, column_values)` mutation that
   updates **only** the output columns on the existing item:

   | Result field | Column | Value format |
   |---|---|---|
   | `risk_level` | AI Risk Level `color_mm8vjgpn` | `{"label": "Low" \| "Medium" \| "High"}` |
   | `risk_explanation` (+ factors, missing info, disclaimer) | Risk Explanation `long_text_mm8vqq3n` | `{"text": ...}` |
   | `onboarding_summary` | Onboarding Summary `long_text_mm8vtgjg` | `{"text": ...}` |
   | `recommended_action` (+ due diligence) | Recommendation Action `long_text_mm8v99qe` | `{"text": ...}` |
   | `human_review_required` | Manual Triage Required `color_mm8v1s9e` | `{"label": "Yes" \| "No"}` |
   | `review_type` | Review Type `color_mm8whrrp` | `{"label": "Auto Triage" \| "Document Review" \| "Compliance Review" \| "Enhanced Due Diligence"}` |
   | `review_reason` | Review Reason `long_text_mm8w9cjk` | `{"text": ...}` |

   **Screening Status** (`color_mm8wva1n`) belongs to compliance. The app sets it to
   `Not Started` only when it is empty, and never overwrites a value compliance has set. If the
   item or the Screening Status column cannot be found, the write fails for that item. The
   mutation response is checked to confirm the expected item was updated.

   No items are ever created, so re-running overwrites the AI columns and never duplicates items.

   **AI Processing Status** (`color_mm8y4j3s`) is only written by the webhook server, through
   `update_processing_status`, which uses `change_simple_column_value` and accepts only
   `Processing`, `Completed` or `Failed`.
4. **Error handling.** monday.com often returns HTTP 200 with an `errors` array (or the older
   `error_code`/`error_message` envelope), so every response body is inspected. Rate-limit and
   complexity errors (`ComplexityException`, `RATE_LIMIT_EXCEEDED`, etc.), HTTP 429/5xx and
   network errors are retried (up to 5 attempts) with exponential backoff capped at 30s, using
   `retry_in_seconds` / `Retry-After` when provided. 401/403 fail immediately with a clear message.
   The token is only ever sent in the `Authorization` header and is redacted from logs.

## How the AI pipeline works

Both stages use `generate_structured` in `src/llm_client.py`. It asks the model for JSON that
matches the Pydantic model's JSON Schema and validates the response with Pydantic (schema and
guardrails). If validation fails, it retries once and tells the model what was wrong. If the second
attempt also fails, it raises `AIResponseError`. Invalid output is never written to monday.com.

`ClaudeCompleter` calls the Claude Messages API with server-side model fallback enabled. A refusal,
a response truncated at `max_tokens`, or an empty response is raised as `AIResponseError`. The
Anthropic SDK itself retries 408/409/429/5xx and connection errors.

**Stage 1: risk assessment** (`src/risk_assessment.py`) returns a `RiskAssessment` with
`risk_level`, `risk_factors` (each with cited `evidence` and `severity`), `risk_explanation`,
`missing_information`, `recommended_due_diligence`, `review_type`, `review_reason` and
`human_review_required`. The prompt requires the model to:

- use only facts in the application. Blank fields are shown to it as `NOT PROVIDED` and must be
  listed as missing, not guessed;
- treat sanctions, PEP, adverse-media and watchlist screening as **outstanding**, and never claim
  it was performed, passed or returned no matches (it may recommend screening);
- never approve or reject a client;
- treat large AUM or many services as operational complexity, not compliance risk;
- never assume a source of funds the application does not establish;
- choose a review type and explain it in 1–3 sentences in `review_reason`.

**Review routing.** The model picks one of four review types:

| Review type | When | Human review |
|---|---|---|
| `AUTO_TRIAGE` | LOW risk, no material red flags, inconsistencies or critical documentation gaps | No |
| `DOCUMENT_REVIEW` | Material missing documentation that prevents reliable verification | Yes |
| `COMPLIANCE_REVIEW` | Specific compliance concerns needing specialist review | Yes |
| `ENHANCED_DUE_DILIGENCE` | Serious or multiple compliance risk indicators | Yes |

Routine outstanding screening and standard onboarding documents do not by themselves stop an
application from being auto-triaged. `AUTO_TRIAGE` does not mean screening is complete, KYC is
approved or the account can be opened.

**Stage 2: summary** (`src/summary_generator.py`) is a separate LLM call that receives the original
application **and** the completed risk assessment. It produces the client overview, onboarding
complexity (with rationale), key compliance considerations, missing documentation, next steps and
a recommended action. It is told not to change the risk level, and to list screening under
compliance considerations and next steps, not under missing client documentation.

**Deterministic safeguards on top of the LLM** (`src/models.py`, `src/risk_assessment.py`):

- *Guardrail validation.* Every text field of both stages is checked sentence by sentence. Text
  that claims screening was performed, passed, cleared the client or produced results, or that a
  client is approved, cleared or rejected, fails validation and triggers the retry. Negated
  statements ("No sanctions screening has been performed") and instructions ("Ensure PEP screening
  is completed before account opening") are allowed. The error reported back to the model names
  only the field and the matched words.
- *Review-routing policy* (`apply_review_policy`). The model's choice is overridden where needed:
  HIGH risk is always `ENHANCED_DUE_DILIGENCE`; MEDIUM risk can never be `AUTO_TRIAGE` (it becomes
  `COMPLIANCE_REVIEW`); LOW risk can't be `ENHANCED_DUE_DILIGENCE` (it becomes
  `COMPLIANCE_REVIEW`). When an override happens it is logged and the reason is prefixed to
  `review_reason`. `human_review_required` is always derived from the final review type: `No`
  only for `AUTO_TRIAGE`.
- Enum normalisation (`"high"` → `HIGH`) and a 2,000-character truncation for long-text columns.
- A fixed disclaimer is appended to the Risk Explanation column: *"AI-generated preliminary
  triage. No sanctions/PEP screening was performed."*

**Failure isolation.** Each application is processed independently. A failure at any stage
(`risk_assessment`, `summary_generation`, `result_validation`, `write_back`) is logged, recorded in
the output JSON with its stage, and processing continues with the next item.

## Running tests

```bash
pytest -q tests
```

Pass the `tests` directory explicitly. A bare `pytest` from the project root also collects
`test_drive_upload.py`, which uploads a real file to Google Drive when it is imported.

The 77 tests use mocked HTTP sessions and a fake LLM, so they need no credentials and make no
network calls. The webhook server and Google Drive upload have no automated tests yet. The tests
cover:

- GraphQL errors returned with HTTP 200, and the older error envelope;
- retries on 429, complexity and network errors, and giving up after the retry limit;
- authentication failures not being retried, and the token being sent only in the header;
- pagination and `--limit`;
- column mapping by title, excluding output columns;
- write-back touching only the output columns, through `change_multiple_column_values`;
- Screening Status being initialised when empty and never overwritten;
- truncation of long-text values;
- AI output validation and retry;
- the guardrails: forbidden claims rejected, legitimate screening statements allowed, each
  sentence and field judged on its own;
- the review-routing policy;
- the summary prompt including the risk assessment;
- dry-run never writing to monday.com;
- one failing item not stopping the batch;
- config, placeholder handling and secret redaction;
- an end-to-end CLI run on the sample file.

## Known limitations

- **No real screening.** This is AI triage of free text only. It does no identity verification,
  sanctions/PEP/adverse-media screening or document checks.
- **Title-based column discovery.** If the board's column titles differ a lot from the aliases in
  `INPUT_FIELD_ALIASES`, fields will be reported as not found. Add an alias or rename the column.
- **Output column IDs are fixed.** The output and Screening Status column IDs are set in
  `src/config.py`. A different board needs those IDs updated.
- **The status labels must already exist.** `Low`/`Medium`/`High`, `Yes`/`No`, the four review type
  labels and `Not Started` must exist on their status columns, because the mutation does not create
  labels.
- **Keyword guardrails are heuristic.** They catch common phrasings, not every possible one.
- **The LLM is non-deterministic.** Re-runs may produce different wording or, at the margins, a
  different risk level or review type. Every run reprocesses every item, with no change detection.
- **`--application-id` reads the whole board** before filtering, which is slow on large boards.
- **`--item-id` reads the whole board** too, so every webhook-triggered run reads all items.
- **Processing is sequential.** One item at a time is fine for a demo, but slow for thousands.
- **Webhook jobs run in-process.** Background tasks are lost if the server restarts, leaving the
  item stuck on `Processing` and its SQLite row on `processing`, which blocks re-triggering.
- **Failed webhook jobs are not retried.** The SQLite claim is permanent; delete the row to retry.
- **Archive failure marks the item `Failed`.** In webhook mode a Google Drive error makes
  `main.py` exit `1`, even if the results were already written back to monday.com.
- **Webhook secret in the URL.** The secret is a path segment, so it can appear in proxy or access
  logs. monday.com's signed JWT `Authorization` header is not verified.
- **Webhook column ID is fixed.** The AI Processing Status column ID and trigger label are set in
  `webhook_server.py` and `src/monday_client.py`.
- Long-text columns are capped at 2,000 characters, so very long outputs are truncated.

## Production hardening recommendations

- **Trigger:** the webhook server is a first step. Move processing behind a durable queue (SQS,
  Pub/Sub) with retries and a dead-letter queue, replace SQLite with a shared store, verify
  monday.com's JWT signature, and call the pipeline directly instead of through a subprocess.
- **Change detection:** store a hash of the input fields and skip unchanged items. Add a "Processing
  status" column (Pending / Processed / Error) for visibility on the board.
- **Targeted lookups:** query items by Application ID (`items_page_by_column_values`) instead of
  reading the whole board.
- **Secrets:** use a secrets manager (AWS Secrets Manager, Vault) and an OAuth monday.com app
  instead of a personal token, with token rotation and least-privilege scopes.
- **Compliance integration:** feed results into real screening providers (sanctions, PEP, adverse
  media) and a case-management system. Keep a human sign-off as the only approval path.
- **Auditability:** persist prompts, model versions, raw responses and validated outputs in an
  immutable audit store. Version the prompts and keep the model pinned.
- **Evaluation:** build a labelled set of fictional applications, run regression evals on every
  prompt or model change, and monitor the drift in the risk level and review type distributions.
- **Data protection:** minimise and redact PII before LLM calls, use an enterprise LLM endpoint with
  zero data retention, and confirm data residency requirements.
- **Stronger structured output:** use the provider's strict JSON-schema mode in addition to Pydantic
  validation. Add an LLM-as-judge or classifier guardrail alongside the regex checks.
- **Operations:** use structured JSON logs, metrics (latency, failure rate by stage, token cost),
  alerting, concurrency with rate-aware throttling against monday.com's complexity budget, and CI
  running the tests and a linter.

## Project structure

```
crestview-ai-onboarding/
├── main.py                    # CLI entry point
├── webhook_server.py          # FastAPI webhook: runs main.py --item-id on "Ready for AI Review"
├── webhook_store.py           # SQLite job store for webhook de-duplication (webhook_events.db)
├── google_drive.py            # Google Drive OAuth + JSON upload (run directly to test auth)
├── test_drive_upload.py       # Manual Drive upload smoke test (creates a real file)
├── reset_board.py             # Clears the AI output columns (preview unless --execute)
├── src/
│   ├── config.py              # env loading, output/screening column IDs, log redaction
│   ├── models.py              # Pydantic models + guardrails
│   ├── monday_client.py       # GraphQL client: read, paginate, map, write back
│   ├── llm_client.py          # Claude wrapper + validate/retry helper
│   ├── risk_assessment.py     # Stage 1 + review-routing policy
│   ├── summary_generator.py   # Stage 2
│   └── pipeline.py            # Orchestration and per-item error isolation
├── sample_data/applications.json
├── tests/
├── output/
├── .env.example
├── requirements.txt
└── README.md
```
