# Quarry

Tender monitoring with self-service accounts, private campaigns and results,
Email/Telegram notifications, and administrator management. The existing Sources,
Tenders, Campaigns, Matching and Activity workspace remains available to administrators
and explicitly enabled local operators. See [self-service setup and demo](docs/operations/SELF_SERVICE.md).

## Documentation

Start with the [documentation index](docs/README.md) for the overview, architecture, user and administrator guides, deployment, SRS matrix and evidence. **READY WITH RESTRICTIONS** applies only to a limited private-network pilot after deployment-specific checks; public production readiness is not established. Older documents may be historical.

## Install

Requirements: Python 3.12+, uv, Node.js 22.12+ with npm, PostgreSQL
(Compose uses PostgreSQL 18).

```sh
uv sync --locked
cd frontend && npm ci --no-audit --no-fund && cd ..
uv run --locked playwright install --with-deps chromium
cp -n .env.example .env
```

Set `DATABASE_URL`. Tests require a separate `TEST_DATABASE_URL` with `test` in
its database name. Compose additionally needs `POSTGRES_PASSWORD`; keep existing
volume, role and database settings. Default connections use port 54329 on
127.0.0.1. URL-encode passwords and keep credentials in `.env`.

For a new Compose installation:

```sh
docker compose up -d --wait postgres
docker compose exec postgres createdb -U tender tender_test
uv run --locked alembic upgrade head
```

Existing installations require a **fresh database** for baseline `0001_quarry`.
Do not run a normal upgrade against the old revision chain or stamp it over an
old schema. Stop workers, back up and restore-test the old database, create an
empty database, apply the baseline, then import retained records using the
[database rebuild procedure](docs/operations/DATABASE_REBUILD.md). Keep the old
database and backup until verification succeeds.

Manual rules matching is always available. `QUARRY_MATCHING_ENABLED` controls
publication-triggered automatic work only. Automatic matching defaults off per
campaign; select Rules or Rules + AI and authorize automatic AI separately.
Web AI matching requires `QUARRY_AI_SEMANTIC_ENABLED=true`, a configured provider/model
and its server-side API key. Manual CLI AI commands remain explicit requests.
Both web and CLI matching enforce shared spending protections. Normal tests use fake providers.

In Campaigns, configure shared lifetime and campaign USD limits and conservative
current token prices for the exact configured provider/model. Global and campaign
limits default to zero. Reservations use a UTF-8 byte bound plus framing allowance,
at most 600 output tokens, and serialized database transactions. Actual reported
token usage reconciles estimates; missing usage, failures and crashes retain the
reservation. No paid request is retried automatically. A reservation is unique per
job, including concurrent workers; forced manual reevaluation creates new paid work.
If provider usage exceeds its reservation, further AI pauses for inspection.

Without known prices, AI stays blocked unless explicit global **and** campaign
lifetime request ceilings are configured. This alternative bounds requests and
tokens, **not dollar charges**; unpriced costs display as unavailable. Dollar limits
apply to priced work. Prices are operator supplied, so stale/incorrect rates and
provider violations cannot guarantee actual invoice amounts. Costs shown are
estimates, never exact charges. Unknown-cost calls remain counted across retries.
Per-run request/token limits and input bounds also apply. Increasing a limit and
resuming pending work does not reset lifetime usage.

## Start

From repository root (builds and serves the browser interface):

```sh
make serve
```

Open http://127.0.0.1:8000 and register or sign in. Stop with Ctrl+C; data remains
in PostgreSQL. Restart with the same command. This serves both API and built UI.
For frontend development with hot reload, use a second terminal:

```sh
cd /home/s0me2/Desktop/Beeline/quarry
make frontend-dev
```

Open http://127.0.0.1:5173; leave the backend running in the first terminal.
If an old terminal reports `ENOENT ... uv_cwd` after replacing the frontend,
its working directory was deleted. Run the absolute `cd` above to reconnect it
to the current directory before invoking npm or Make.

When reusing a PostgreSQL volume created by another Compose project, keep
`POSTGRES_VOLUME` pointing to that volume and set `POSTGRES_VOLUME_EXTERNAL=true`
in `.env`. Fresh installations leave this false so Compose creates the volume.
API binding accepts only localhost/loopback addresses;
Vite and Compose also bind to loopback. External browser origins are rejected.
Database/provider credentials remain on the server. Public registration creates ordinary
users; administrator creation requires the explicit bootstrap command documented in
[self-service operations](docs/operations/SELF_SERVICE.md). Existing campaigns without
account ownership remain administrator/operator resources.

## Workflows

- **Sources:** add a website URL. Quarry investigates, generates a recipe, checks
  real extraction against independent website evidence. Customer monitoring requires both administrator approval and a valid, independently verified active recipe.
  Public URL-only generation has repeatedly failed on real sites; see [known issues](docs/KNOWN_ISSUES.md).
  Advanced JSON recipe import remains available. See [automatic recipes](docs/operations/AUTOMATIC_RECIPES.md).
  Enable/disable at any time and scrape manually. Source IDs and recipe versions
  are generated internally. Failed replacements preserve the working recipe.
  Change the tender limit for each scrape without saving or revalidating the recipe.
  Recipe checks use three records by default; request/time/download safeguards stay
  active. Automatically generated sources join scheduled monitoring when the global
  scheduler is enabled; sample limits remain in force.
- **Tenders:** browse/search all collected records, inspect products, evidence,
  provenance and readable version history, or manually create a tender.
  Request IDs are generated internally. Domain validation and normal versioning
  apply to manual entries.
- **Campaigns:** create/edit requirements and activate/deactivate. Criteria
  versions remain immutable; the editor tracks the current version. Automatic
  matching controls and shared AI limits live here. Creation, edits and activation never
  backfill historical tenders.
- **Matching:** select an active campaign and up to 500 tenders; run Rules directly
  or review/confirm paid AI evaluation; inspect saved decisions, explanations,
  evidence and provider errors. AI uses at most one model request per selected pair, no
  escalation/retries. Pending work and completed counts appear with estimated costs.
- **Activity:** inspect persisted operations, progress, results and failures.
  Interrupted work is reconciled; paid failures are not automatically repeated.
  Resume queued work and paused automatic matching under current safeguards.
  On-demand scraping dispatches matching even with polling disabled; no scraping
  schedule is needed. Each publication chunk covers at most 50 changed tenders;
  an automatic execution handles at most 100 pending jobs (manual selections: 500). At most ten chunks dispatch after
  a scrape; additional chunks remain queued. Unchanged observations queue nothing.
  Completed equivalent jobs are reused. Accepted matching-input and quality changes
  trigger work even when legacy source policy labels them TRACK; ignored/volatile
  fields remain unchanged. URL/document-only changes reuse equivalent evaluations.
  Hard restrictions run before AI. Matching
  failures leave successful collection and completed decisions intact.

If startup fails, check `.env` and start PostgreSQL (`docker compose up -d --wait
postgres` for Compose). A new empty database needs `uv run --locked alembic upgrade
head`; an older schema needs the explicit rebuild procedure above. Never stamp
an old schema as current. If the port is occupied, stop the other Quarry instance
or set `QUARRY_API_PORT`. Installation/build failures: rerun `uv sync --locked`
and `cd frontend && npm ci --no-audit --no-fund` with the required runtimes.

Recipe engines, HTTP/browser limits, normalization, health gates, independent
witnesses, offline replay, tender history, matching evidence and atomic database
publication remain intact. Source-site credentials/sessions are acquisition
configuration, distinct from Quarry account authentication.

## Verification

```sh
make frontend-build
make check
make test-api
make test-product-e2e
make schema-check
make build
```

Tests use random schemas in the dedicated PostgreSQL test database, loopback
sites, Chromium and fake model clients. Do not invoke live benchmarks or paid
providers without explicit authorization. `make test-unit` selects tests without
database/browser requirements. URL-only source generation uses existing AI providers and validation gates. CLI recipe diagnostics and explicit bounded
operations remain available through `uv run --locked quarry-manual --help`.

See [architecture](docs/architecture/ARCHITECTURE.md),
[configuration](docs/operations/CONFIGURATION.md) and
[testing](docs/operations/TESTING.md).

### Tender attachments

Accepted collections process attachment URLs after tender publication. Results are available
through `SqlTenderRepository.documents(tender_id)`; each result has a `status`, extracted text,
and an error when processing failed. Removed URLs remain stored but are excluded from this query.
Only use text from results with `status == "success"`; a failed recheck can retain older text.
Successful and failed attempts are retried after 24 hours. Successful downloads use HTTP
validators when available; unchanged content is not extracted again.

Install the `tesseract-ocr` and `antiword` system packages on collection workers. Install
`tesseract-ocr-eng`, `tesseract-ocr-rus`, and `tesseract-ocr-uzb` language data for the
languages needed by your sources; OCR uses the installed subset. PDF, DOCX, XLSX, XLS,
TXT, JPEG and PNG use Python dependencies. Legacy DOC uses `antiword`. Missing executables
or language data produce document failures without rejecting the tender. Downloads require
public HTTP(S) addresses; private-network and authenticated attachments are not supported.
Processing respects collection request, byte, and time budgets, with additional per-file
and extraction limits. Successfully extracted document text is passed into relevance matching; failed files remain visible without discarding the tender.
