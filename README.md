# Rung — Data Ingestion

UK-first compensation and labour market data ingestion pipelines for the Rung app.

> Project root: `C:\Users\Matth\Downloads\Rung\rung-ingestion\`.

## Structure

```
ingestion/
  nomis/            # ONS ASHE via Nomis API (tier 1, annual)
  reed/             # Reed Jobseeker API live listings (tier 1, daily)
  hmrc_paye/        # ONS/HMRC earnings from PAYE RTI (tier 1, monthly)
  stack_overflow/   # Stack Overflow Developer Survey (tier 2, annual)
  nhs/              # NHS Agenda for Change pay scales (tier 2, annual)
  ucu/              # Higher-education single pay spine (tier 2, annual)
  civil_service/    # Civil Service pay bands (tier 2, annual)
  ons_earn/         # ONS EARN01/02/03 series (tier 3, monthly)
  london_datastore/ # London borough earnings (tier 3, annual)
  hmrc_spi/         # HMRC Survey of Personal Incomes (tier 3, annual)
  local_gov/        # Local gov senior-officer disclosures (tier 4, quarterly)

shared/
  db.py             # Postgres connection + bulk upsert helpers
  normalization.py  # Period → annual conversions (UK conventions)
  models.py         # CompensationObservation dataclass + enums

schema/
  migrations/       # SQL migrations (DDL for compensation_observations, etc.)

.github/workflows/  # One scheduled GitHub Action per source
data/
  raw/              # Immutable raw downloads, per-source subfolders
  staged/           # Parsed intermediate outputs (git-ignored)
```

## Conventions

Every ingestion module exposes:

- `fetch.py` — pulls raw data from source, writes to `data/raw/<source>/<date>/`
- `parse.py` — transforms raw data into `CompensationObservation` rows
- `load.py` — idempotent bulk upsert via `(source_id, source_reference)`
- `run.py` — CLI orchestrator calling fetch → parse → load

Each source is independently runnable, e.g. `python -m ingestion.nomis.run`.

Scheduling is handled via `.github/workflows/<source>.yml` at the source's natural cadence (annual / monthly / daily).

## Local setup

```bash
pip install -r requirements.txt
cp .env.example .env       # fill in API keys and database URL
python -m ingestion.nomis.run --dry-run     # first sanity check
```

Required env vars:

- `DATABASE_URL` — Postgres connection string (Supabase free tier works fine)
- `REED_API_KEY` — from https://www.reed.co.uk/developers (free registration)
- `NOMIS_API_KEY` — optional, from https://www.nomisweb.co.uk/myaccount/userjoin.asp

## Running in CI (zero-cost path)

1. Push this folder to a GitHub repo (public repo = unlimited free Actions minutes).
2. Add two repo secrets under *Settings → Secrets and variables → Actions*:
   - `DATABASE_URL`
   - `REED_API_KEY`
3. Workflows in `.github/workflows/` will run on their natural cadence.
4. Use the *workflow_dispatch* trigger to backfill any source manually.
