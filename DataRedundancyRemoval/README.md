# Data Redundancy Removal System

Cloud Computing Internship, **Task 1: Data Redundancy Removal System**.

This is a cloud web service that checks every incoming record against the data already stored. It classifies
each record as **unique**, **redundant** or **false positive** (or **invalid**) and appends only unique, verified
records to the database. Exact copies, formatting variants and typo duplicates never reach the cloud database.

## How it meets the task requirements

| Requirement | How it's done |
|---|---|
| Identify and classify data as redundant or false positive | `dedup.classify()` labels each record `unique`, `redundant`, `false_positive` or `invalid`, and gives the reason and similarity score |
| Validation mechanism against existing data | Field validation, then fingerprint, email/phone and fuzzy comparison with stored records. `/api/check` runs this without saving |
| Prevent duplicates in the cloud database | Duplicates are blocked by the pipeline, and the database has `UNIQUE` constraints on fingerprint, email and phone, so the same record sent twice at once is stored only once (tested with 8 threads) |
| Append only unique, verified data | Only `unique` and `false_positive` records are inserted. Every decision is written to an audit log |
| Accuracy and efficiency | Normalization, blocking keys and indexes, so each record is compared only with plausible matches. `clean_csv.py` removes redundancy from existing files |

## The pipeline

```
incoming record
   │
   ├─ 1. VALIDATE   name present, email valid, phone 10–15 digits ──── fail → INVALID (rejected)
   ├─ 2. NORMALIZE  lowercase, trim, remove accents, phone → last 10 digits
   ├─ 3. EXACT      SHA-256 fingerprint already stored? ────────────── yes → REDUNDANT (blocked)
   ├─ 4. KEY MATCH  same email or same phone already stored? ─────────  yes → REDUNDANT (blocked)
   ├─ 5. FUZZY      compare with candidates sharing a blocking key
   │     ├─ similar name AND almost identical email/phone (typo) ──→ REDUNDANT (blocked)
   │     ├─ weighted similarity ≥ 0.60 (looks alike, IDs differ) ──→ FALSE POSITIVE (stored, flagged)
   │     └─ otherwise ─────────────────────────────────────────────→ UNIQUE (stored)
   └─ 6. INSERT     database UNIQUE constraints are the final guard
```

**What "false positive" means here:** a record that looks like a duplicate but isn't one. For example, two different
people both named *John Smith* with different emails and phones. A simple name check would wrongly drop the
second person. This system keeps them and flags them for review.

**Similarity:** `difflib.SequenceMatcher` per field, weighted name 40%, email 30%, phone 20%, city 10%.
Names are also compared with their words sorted, so "Wei Chen" matches "Chen Wei".

**Efficiency:** each record is compared only with **candidates** that share a blocking key (first 4 letters
of the name, first 4 letters of the email, or last 6 phone digits), at most 200 of them, and each key is indexed.
The cost of checking a record therefore stays roughly flat as the table grows.

## Example (sample_data.csv, 20 rows)

| Input | Result | Why |
|---|---|---|
| `JOHN SMITH, John.Smith@GMAIL.com, 415-555-0101` | redundant | same fingerprint after normalization |
| `Priya Sharma, priya.s.work@gmail.com, 9876543210` | redundant | phone already exists (country code ignored) |
| `Maria Garcia, maria.garcia@gmial.com, …679` | redundant | typo near-duplicate |
| `Wei Chen` vs `Chen Wei` | redundant | reordered name + near-identical email |
| `John Smith, jsmith.dev@protonmail.com, London` | false positive | same name, different person → stored |
| `Sara Ali, sara.ali@` | invalid | email is invalid |

Result: 20 received → **10 stored**, 7 duplicates blocked, 3 invalid rejected. All 20 match their expected labels.

## Project structure

```
DataRedundancyRemoval/
├── dedup.py            Validation, normalization, similarity, classification
├── database.py         SQLAlchemy store: SQLite locally, PostgreSQL in the cloud
├── app.py              Flask REST API
├── templates/index.html  Dashboard (add record, CSV upload, records, audit log)
├── clean_csv.py        Command-line tool: clean an existing CSV file
├── sample_data.csv     20 test rows with expected labels
├── test_dedup.py       19 unit, API and concurrency tests
└── requirements.txt / Procfile / Dockerfile
```

## Run locally

```bash
python -m venv venv
venv\Scripts\activate          # Windows  (macOS/Linux: source venv/bin/activate)
pip install flask sqlalchemy
python app.py
```

Open http://localhost:5001. Try adding a record twice, or upload `sample_data.csv`.

Clean a file from the command line:

```bash
python clean_csv.py sample_data.csv clean.csv
```

## Test

```bash
python -m unittest -v
```

## API

| Method | Path | Result |
|---|---|---|
| POST | `/api/records` | `{"name","email","phone","city"}` → 201 stored / 409 redundant / 422 invalid |
| POST | `/api/check` | same classification, **nothing saved** |
| POST | `/api/upload` | multipart CSV (`file`) → summary + per-row decisions |
| GET | `/api/records` | stored records |
| GET | `/api/log` | audit trail of every decision |
| GET | `/api/stats` | counts and redundancy rate |
| GET | `/health` | health check |

## Deploy to the cloud

The app reads `DATABASE_URL`. Without it, it uses a local SQLite file. In the cloud, use a managed PostgreSQL
database so the data persists and all servers share it.

**Render (free tier)**
1. Push this folder to GitHub as `DataRedundancyRemoval`.
2. On Render, create a **PostgreSQL** database and copy its *Internal Database URL*.
3. Create a **Web Service** from the repo. Build command `pip install -r requirements.txt`, start command
   `gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT`.
4. Add the environment variable `DATABASE_URL` = the URL from step 2.

**Docker (AWS App Runner + RDS, Google Cloud Run + Cloud SQL, Azure Container Apps + Azure Database for PostgreSQL)**
```bash
docker build -t data-redundancy-removal .
docker run -p 8080:8080 -e DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db data-redundancy-removal
```

The tables are created automatically on first start. The web servers keep no state, so you can run as many
instances as you need. The database constraints stop duplicates even when several instances insert at the same time.

## Tuning

Thresholds are at the top of `dedup.py`: `NAME_MATCH`, `EMAIL_MATCH`, `PHONE_MATCH` and `FALSE_POSITIVE_THRESHOLD`.
