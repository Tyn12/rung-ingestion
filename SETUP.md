# Rung Ingestion — Setup Guide

This is the one-time setup. After this, every data source refreshes automatically on its own schedule, free forever (unless your data exceeds 500 MB, at which point you can upgrade).

**Total time:** ~45 minutes, most of it waiting for emails.
**Cost:** £0. All services used have permanent free tiers generous enough for this project.

---

## Stage A — Prerequisites check

You need three things installed on your Windows machine. Run this in Command Prompt or PowerShell to check:

```
python --version
git --version
```

- **Python:** ✅ you already have 3.14 installed.
- **Git:** if you see "'git' is not recognized…" you need to install it. Download **Git for Windows** from https://git-scm.com/download/win and run the installer with all default options. Close and reopen your terminal.

Nothing else needs downloading — Supabase and GitHub are both browser-based.

---

## Stage B — Create the Supabase database (5 min)

### B1. Sign up

1. Go to https://supabase.com
2. Click **Start your project** → sign in with GitHub (or email).
3. If it asks about an organisation, let it create a Personal org.

### B2. Create the project

1. Click **New project**.
2. Fill in:
   - **Name:** `rung`
   - **Database Password:** click **Generate a password**, then **copy it somewhere safe** (you won't see it again — a password manager, a text file, whatever). You'll need it in 30 seconds.
   - **Region:** `West EU (London)` — closest to your data geographically.
   - **Plan:** Free.
3. Click **Create new project**. It takes ~2 minutes to provision — you'll see a progress screen.

### B3. Copy the connection string

1. Once the project is ready, click the **Connect** button near the top of the dashboard.
2. In the dialog you'll see three URI variants. **Pick "Session pooler"** — not Direct connection, not Transaction pooler.
   - **Why not Direct?** The free tier's direct connection is IPv6-only, and GitHub Actions runners are IPv4-only. Your scheduled workflows would fail every time.
   - **Why not Transaction pooler?** It doesn't support prepared statements, which psycopg2 uses under the hood for bulk upserts.
   - **Session pooler is the sweet spot:** IPv4, supports everything our code needs.
3. Click the **URI** tab (as opposed to .NET, Python, JDBC, etc. — those are just reformatted versions of the same thing).
4. You'll see a string like:
   ```
   postgresql://postgres.xxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-eu-west-2.pooler.supabase.com:5432/postgres
   ```
5. Replace `[YOUR-PASSWORD]` with the password you saved in B2.
6. **Keep this string open in a tab** — you'll paste it twice (into `.env.local` and into a GitHub secret).

### B4. Put the connection string in your local .env

1. Open `C:\Users\Matth\Downloads\Rung\rung-ingestion\.env.local` in Notepad (or any editor).
2. Find the line starting `DATABASE_URL=`
3. Replace it with your actual string, for example:
   ```
   DATABASE_URL=postgresql://postgres:YourActualPasswordHere@db.abcd1234.supabase.co:5432/postgres
   ```
4. Save.

### B5. Run the schema migration

**No download needed** — Supabase has a built-in SQL editor.

1. In Supabase, click the **SQL Editor** icon in the left sidebar (looks like `</>`).
2. Click **New query**.
3. Open `C:\Users\Matth\Downloads\Rung\rung-ingestion\schema\migrations\0001_compensation_observations.sql` in Notepad.
4. Select all (Ctrl+A), copy (Ctrl+C), paste into the Supabase SQL editor, click **Run** (or Ctrl+Enter).
5. You should see `Success. No rows returned`. The `compensation_observations` table and its partitions are now live.

**Verify:** In the left sidebar click **Table Editor**. You should see `compensation_observations` and `dim_source` tables listed.

---

## Stage C — Test locally before going live (5 min)

Before wiring anything up to GitHub, confirm the pipeline actually talks to your new database.

1. Open a new terminal and `cd` to the project:
   ```
   cd /d C:\Users\Matth\Downloads\Rung\rung-ingestion
   ```
2. Install dependencies (one-off):
   ```
   pip install -r requirements.txt
   ```
3. Do a seed-only UCU ingest (no network fetch, just inserts the hand-curated spine):
   ```
   python -m ingestion.ucu.run --seed-only --year 2023
   ```
   Expected output:
   ```
   [ucu:run] Seed-only mode: emitted 51 spine points for 2023.
   [ucu:run] Upserted 51 observations.
   ```
4. Back in Supabase **Table Editor**, click `compensation_observations`. You should see 51 rows with `source_id = ucu_pay_spine`.

**If this works, the rest of the pipeline will too.** Everything else follows the same shape.

---

## Stage D — Push to GitHub (10 min)

GitHub Actions is where the scheduling actually happens. Free unlimited minutes on public repos; 2,000 minutes/month on private (we'll use well under 100).

### D1. Create a GitHub account

1. Go to https://github.com/join and sign up if you don't already have one.
2. Verify your email.

### D2. Create an empty repo

1. Click the **+** button top-right → **New repository**.
2. Fill in:
   - **Repository name:** `rung-ingestion`
   - **Description:** `UK compensation data ingestion pipelines`
   - **Public** (free unlimited Actions minutes) — or Private if you prefer (still free up to 2,000 min/mo).
   - **Do NOT** check "Add a README", "Add .gitignore" or "Add a license" — we already have a README.
3. Click **Create repository**. You'll land on a page titled "Quick setup" with commands.

### D3. Push your local folder

Open Command Prompt or PowerShell, then copy-paste these lines one at a time:

```
cd /d C:\Users\Matth\Downloads\Rung\rung-ingestion
git init
git branch -M main
git add .
git commit -m "Initial commit: 11 UK compensation ingestion pipelines"
git remote add origin https://github.com/YOUR-USERNAME/rung-ingestion.git
git push -u origin main
```

Replace `YOUR-USERNAME` with your GitHub username (visible on the Quick setup page GitHub just showed you — you can also copy the exact line from there).

**First push** will prompt for GitHub authentication. The simplest path:
1. A browser window pops up asking you to authenticate — click through it.
2. Or, if it asks for a password in the terminal, don't use your GitHub password — use a Personal Access Token (GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token, tick `repo` scope, copy the token, paste that as the password).

Refresh your GitHub repo page — you should see all the files.

### D4. ⚠️ Security sanity check

Open your GitHub repo in a browser and check:
- `.env.local` is **NOT** in the file list (it shouldn't be; `.gitignore` blocks it). If it is there, **stop**, delete it via GitHub's web UI, and rotate both your Supabase password and Reed API key.
- `requirements.txt`, `README.md`, the `ingestion/` and `shared/` folders, etc. should all be visible.

---

## Stage E — Wire up secrets (5 min)

Your workflows need two secrets to connect to the database and Reed API. GitHub stores these encrypted, separate from your code.

1. In your GitHub repo, go to **Settings** (top nav) → **Secrets and variables** → **Actions** (left sidebar) → **New repository secret**.
2. Add the first secret:
   - **Name:** `DATABASE_URL`
   - **Secret:** paste the full `postgresql://...` connection string from Stage B3 (with your real password).
   - Click **Add secret**.
3. Add the second secret:
   - **Name:** `REED_API_KEY`
   - **Secret:** `634e7922-4d04-4a61-8262-113830a6043e`
   - Click **Add secret**.

Now Settings → Secrets → Actions shows two secrets. GitHub Actions workflows reference them as `${{ secrets.DATABASE_URL }}` etc. — they never appear in logs.

---

## Stage F — Trigger the first real runs (5 min)

Time to watch each source fire for the first time.

### F1. Manual trigger

1. In your GitHub repo, click **Actions** (top nav). You'll see all 11 workflows listed on the left.
2. Click **Ingest Nomis ASHE** (for example).
3. On the right, click **Run workflow** → **Run workflow** (leave defaults).
4. Wait ~30 seconds, then refresh. You'll see a green checkmark (success) or red X (failure).
5. Click into the run, then into the `ingest` job, to see the full log of exactly what happened.

### F2. Recommended first-run order

Run these one-by-one to spread out any issues:

1. **Ingest UCU/UCEA Pay Spine** — safest (uses seeded fallback, will always succeed)
2. **Ingest Civil Service Pay Bands** — same (seed fallback)
3. **Ingest HMRC Survey of Personal Incomes** — same
4. **Ingest Stack Overflow Developer Survey** — fetches a real file
5. **Ingest NHS AfC Pay Scales** — fetches & parses HTML
6. **Ingest ONS Average Weekly Earnings** — fetches three real files
7. **Ingest HMRC PAYE RTI** — fetches ONS bulletin
8. **Ingest London Datastore Earnings** — CKAN + XLSX
9. **Ingest Nomis ASHE**
10. **Ingest Reed Jobseeker** — uses REED_API_KEY secret
11. **Ingest Local Government Senior Salaries** — will be a no-op until you register council URLs (no error, just "No councils have URLs registered").

After each run, go back to Supabase Table Editor → `compensation_observations` to see rows land.

### F3. Let the schedulers take over

Once you're happy with a workflow, you don't need to do anything more. Each one will run on its cron schedule (see `.github/workflows/*.yml`), and upserts are idempotent so reruns are safe.

---

## Stage G — Optional: extend

### G1. Add more councils (Source 11)

To get Local Government data flowing, you need to register council CSV URLs:

1. Open `ingestion/local_gov/fetch.py`.
2. In `COUNCIL_REGISTRY`, fill in the `url=` field for each council by visiting their open-data page (e.g. https://www.birmingham.gov.uk/openData) and finding their annual senior-salaries CSV.
3. Commit and push; next scheduled run will pick them up.

### G2. Register new-year URLs for manual sources

Sources 6, 7, 10 have "KNOWN_*" dicts at the top of their `fetch.py`. When a new year is published, add the URL there and commit. Until you do, the seeded baseline keeps flowing.

### G3. Check storage usage

Supabase free tier = 500 MB. The heaviest source is ONS EARN (full time series ~= 50 MB/year). You should comfortably fit 5-10 years of data. Check **Database → Storage** in Supabase.

---

## Troubleshooting

**"Could not open requirements file"** — you're not in the right folder. Run `cd /d C:\Users\Matth\Downloads\Rung\rung-ingestion` first.

**"REED_API_KEY is not set"** — your `.env.local` wasn't loaded. Make sure the file is named exactly `.env.local` (not `.env.local.txt`, which Notepad sometimes adds), and that it sits in the project root.

**Workflow fails with "connection refused"** — the Supabase project has paused due to inactivity (free tier pauses after 7 days of no queries). Log into Supabase and click **Restore** on the project.

**Workflow fails with "permission denied for schema public"** — your database role is wrong. In Supabase SQL editor, run:
```sql
GRANT ALL ON SCHEMA public TO postgres;
GRANT ALL ON ALL TABLES IN SCHEMA public TO postgres;
```

**Reed API returns 401** — API key rotated. Generate a new one at https://www.reed.co.uk/developers and update the `REED_API_KEY` repo secret.

**Want to reset everything?** In Supabase SQL editor: `DROP TABLE compensation_observations CASCADE; DROP TABLE dim_source;`, then re-run `0001_compensation_observations.sql`.
