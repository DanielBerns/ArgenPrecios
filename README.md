# ArgenPrecios

**ArgenPrecios** is a standalone, country-level multi-source historical price warehouse, interactive 2-level catalog editor, multi-jurisdiction basket studio, and cross-government CPI engine covering all **24 Argentine jurisdictions (`AR-*`)** and **6 INDEC statistical regions** over **8+ years** within a **500 GB PostgreSQL storage budget**.

> **Strict Zero-Interference Guarantee**: ArgenPrecios runs in its own repository, `uv` environment, runtime directory (`~/Info/ArgenPrecios/`), PostgreSQL instance (`127.0.0.1:5433`), and FastAPI port (`8080`). It accesses external producer artifacts (`sur_en_precios`, `pipeline_price_scrape`, and `hermes_reborn`) strictly **read-only** (`?mode=ro` for SQLite; isolated temporary extraction for SEPA `.zip` archives) and never touches `sur_en_precios`'s regional `AR-U` SQLite database, cron schedule, or PythonAnywhere deployment.

---

## 1. Installation

### Prerequisites

- **Python 3.12+** managed via [`uv`](https://docs.astral.sh/uv/) (`uv 0.10+`)
- **PostgreSQL 16+** (local service or rootless Podman/Docker container via the included `compose.yaml`)
- Sibling local checkout of `../hermes_reborn` (linked as a library dependency in `pyproject.toml`)

### Setup with `uv`

```bash
# Verify uv is installed
uv --version

# Synchronize all runtime and development dependencies into .venv
uv sync
```

---

## 2. Configuration

ArgenPrecios resolves configuration with strict precedence:
1. **CLI flags**: `--config <path>` and `--workdir <dir>`
2. **Environment variables**: `ARGENPRECIOS_WORKDIR`, `ARGENPRECIOS_DATABASE_URL`, `ARGENPRECIOS_SECRET_KEY`, `ARGENPRECIOS_ADMIN_PASSWORD`
3. **Default YAML**: `~/Info/ArgenPrecios/config.yaml` (modeled on [`config/settings.example.yaml`](config/settings.example.yaml))

> **Secret Separation**: Database passwords, session signing keys, and admin credentials are **never** stored in committed YAML files; inject them via environment variables.

### Key Configuration Parameters

| Parameter / Env Variable | Default | Description |
| :--- | :--- | :--- |
| `workdir` / `ARGENPRECIOS_WORKDIR` | `~/Info/ArgenPrecios` | Owned runtime directory for logs, reports, temp extraction, and fallback downloads |
| `ARGENPRECIOS_DATABASE_URL` | `postgresql+psycopg://argenprecios:argenprecios_dev@127.0.0.1:5433/argenprecios` | SQLAlchemy 2.0 `psycopg` v3 PostgreSQL connection URL |
| `ARGENPRECIOS_SECRET_KEY` | Random ephemeral hex (dev) | Signing secret for administrator session cookies and CSRF tokens |
| `sources.sur_en_precios_raw_dir` | `~/Info/sur_en_precios/data/raw` | **Read-only** daily SEPA `.zip` archive directory |
| `sources.pipeline_price_scrape_samples_dir` | `~/Projects/github/DanielBerns/pipeline_price_scrape/samples` | **Read-only** VTEX HTML scrape directory (recovers 13-digit EAN/PLU from JSON-LD `mpn`) |
| `sources.hermes_clean_db_path` | `~/Projects/github/DanielBerns/hermes_reborn/hermes_clean.db` | **Read-only** (`?mode=ro`) master catalog SQLite file |
| `spans.max_bridge_gap_days` | `2` | Maximum missing days bridged within a single `(article, pos, source)` price span |
| `spans.hot_partition_fillfactor` | `85` | Quarterly PostgreSQL partition `fillfactor` enabling Heap-Only Tuple (`HOT`) updates |
| `spans.storage_budget_gb` | `500` | Target multi-administration warehouse disk budget |
| `web.host` / `web.port` | `127.0.0.1` / `8080` | Bind address and port for the FastAPI Intranet Dashboard & Catalog Editor |

Supporting configuration files in [`config/`](config/):
- [`config/settings.example.yaml`](config/settings.example.yaml): Complete annotated configuration template.
- [`config/regional_weights.yaml`](config/regional_weights.yaml): 6 INDEC statistical regions (`GBA`, `PAMPEANA`, `NOA`, `NEA`, `CUYO`, `PATAGONIA`) and all 24 `AR-*` provinces with household expenditure and population weights.
- [`config/government_periods.yaml`](config/government_periods.yaml): Presidential administrations and macroeconomic sub-periods for normalized inflation comparisons.

---

## 3. Initialization

### Step 1: Start Isolated PostgreSQL 16 Instance (Port `5433`)

```bash
# Rootless daemonless Podman (no podman.sock required):
podman run -d --replace --name argenprecios-postgres \
  -e POSTGRES_DB=argenprecios \
  -e POSTGRES_USER=argenprecios \
  -e POSTGRES_PASSWORD=argenprecios_dev \
  -p 127.0.0.1:5433:5432 \
  docker.io/library/postgres:16-alpine \
  -c shared_buffers=256MB -c work_mem=32MB
```

### Step 2: Run Alembic Migrations & Bootstrap Owned Directories

Schema creation always runs through **Alembic** (`alembic upgrade head`):

```bash
export ARGENPRECIOS_DATABASE_URL="postgresql+psycopg://argenprecios:argenprecios_dev@127.0.0.1:5433/argenprecios"
export ARGENPRECIOS_SECRET_KEY="replace-with-32-byte-production-secret"

# Creates ~/Info/ArgenPrecios/*, runs `alembic upgrade head`, pre-creates quarterly partitions,
# and seeds the 24 provinces, 6 INDEC regions, default 2-level taxonomy, and government periods:
uv run argenprecios init

# Or run Alembic directly:
uv run alembic upgrade head
```

### Step 3: Seed Master Catalog from `hermes_reborn` & Create Admin Account

```bash
# Seed 68k+ normalized articles, categories, brands, and tags strictly read-only (?mode=ro)
uv run argenprecios seed-hermes

# Create an administrator account + API Bearer token for the Catalog Editor & Basket Studio
uv run argenprecios create-admin --username admin
```

---

## 4. Execution

### Ingestion Pipelines (4 Data Sources)

```bash
# 1. Daily National SEPA (reads sur_en_precios/data/raw read-only across all 24 provinces)
uv run argenprecios ingest sepa --download-if-missing

# Historical multi-year SEPA backfill (reconciles out-of-order archives automatically)
uv run argenprecios ingest sepa-backfill --dir ~/Info/ArgenPrecios/data/raw/sepa

# 2. Supermarket Web Scraping (VTEX HTML JSON-LD `mpn` EAN recovery + JSONL contract)
uv run argenprecios ingest web-scrape
uv run argenprecios ingest web-jsonl --file ~/Info/ArgenPrecios/data/raw/web_jsonl/coto_2026-04-08.jsonl

# 3. Offline Street Surveys, External SQLite/Parquet Datasets, & Official Macro Series
uv run argenprecios ingest survey --file ~/Info/ArgenPrecios/data/raw/surveys/relevamiento_barrial.xlsx
uv run argenprecios ingest external-db --file ./historico.parquet --profile ./mapping_profile.yaml
uv run argenprecios ingest macro --sync-periods --file ~/Info/ArgenPrecios/data/raw/macro/indec_ripte.csv

# 4. Staged Receipt / PDF Captures (quarantined until human verification)
uv run argenprecios ingest stage-captures --file ./tickets_extraidos.json
uv run argenprecios ingest promote-staged --ids 1,2,3
```

### Analytics Rollups, Baskets & Reports

```bash
# Materialize Stage 1 Jevons variety rollups & Stage 2 Chained Regional/National CPI
uv run argenprecios rollup --from-date 2026-01-01 --to-date 2026-04-08 --frequency monthly

# Import a sur_en_precios YAML basket and evaluate it at the Country or Regional level
uv run argenprecios basket import-yaml --file ../sur_en_precios/sur_en_precios/data/baskets/canasta_basica_alimentos.yaml
uv run argenprecios basket evaluate --uuid cba-adulto-equivalente-v1 --date 2026-04-08 --geo-level country --geo-code AR

# Export HTML, Markdown, CSV, and Parquet national & cross-government briefing reports
uv run argenprecios report --stem informe_abril_2026 --formats html,md,csv,parquet
```

### Intranet FastAPI Dashboard, Faceted Search, Category Editor & Basket Studio

```bash
# Starts Uvicorn on http://127.0.0.1:8080 (OpenAPI docs at /docs)
uv run argenprecios serve --host 127.0.0.1 --port 8080
```

### Python Research SDK (`WarehouseClient`)

```python
from datetime import date
from argenprecios import GeoLevel, WarehouseClient, WeightingScheme

with WarehouseClient.from_config() as client:
    hits = client.search_articles("leche entera", min_active_provinces=5)
    cpi = client.get_cpi_series(geo_level=GeoLevel.COUNTRY, geo_code="AR")
    periods = client.compare_government_periods(weighting=WeightingScheme.EXPENDITURE)
```

### Staggered Cron Scheduling (Zero Interference with `sur_en_precios`)

Schedule ArgenPrecios at **08:30 AM** (2.5 hours after `sur_en_precios`'s `06:00 AM` job) under low CPU/I/O priority:

```cron
30 8 * * * nice -n 15 ionice -c 2 -n 7 /home/dberns/.local/bin/uv run --directory /home/dberns/Projects/github/DanielBerns/ArgenPrecios argenprecios ingest sepa --download-if-missing
```

### Quality Gate & Verification Suite

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/
uv run pytest
```
