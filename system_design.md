# System Design Specification — ArgenPrecios

## 1. Goals & Strict Non-Interference Contract

**ArgenPrecios** builds a **country-level, multi-source historical price warehouse** covering all 24 Argentine jurisdictions (`AR-*`) across **8+ years (2+ presidential administrations)** within a **500 GB PostgreSQL storage budget**, while enforcing **zero operational, code, or resource interference** with `sur_en_precios` and `pipeline_price_scrape`.

> [!IMPORTANT]
> **Zero-Interference Isolation Rules**
> 1. **Standalone Repository & Virtualenv**: Lives in a new independent repository (`/home/dberns/Projects/github/DanielBerns/ArgenPrecios`, Python package `argenprecios`) managed by `uv`, with its own `.venv`, `pyproject.toml`, and `~/Info/ArgenPrecios/` working directory. Zero files in `sur_en_precios` are edited or imported.
> 2. **Database & Host Isolation**: Uses a dedicated **PostgreSQL** database on isolated port `5433`. It never touches `public.sqlite`, `public_red/green.sqlite`, `evolution.sqlite`, `user_data.sqlite`, `control.sqlite`, or PythonAnywhere's 512 MB environment.
> 3. **Read-Only Producer Consumption**:
>    - Reads `~/Info/sur_en_precios/data/raw/YYYY-MM-DD/sepa_YYYY-MM-DD.zip` **strictly read-only**, extracting archives exclusively into its own `~/Info/ArgenPrecios/data/tmp/` directory. If a daily SEPA archive is missing, its fallback `aiohttp` downloader writes only to `~/Info/ArgenPrecios/data/raw/sepa/`.
>    - Reads `pipeline_price_scrape` output folders (`samples/<timestamp>/<chain>/*.html`) **strictly read-only**.
>    - Reads `hermes_clean.db` **strictly read-only** (`?mode=ro`) and imports `hermes_reborn` as a standard Python dependency.
> 4. **Staggered Scheduling**: Cron/systemd timers run outside `sur_en_precios`'s `06:00 AM` window (e.g., `08:30 AM` after regional sync finishes) with `nice`/`ionice` priority lowering so regional jobs always have first claim on CPU and disk I/O.

---

## 2. Four-Pillar Storage Strategy (< 500 GB Over 8+ Years)

1. **Lossless Temporal Run-Length Encoding (`price_spans`)**:
   - Stores contiguous intervals `(article_id, pos_id, source_id, valid_from, last_seen, observed_days, list_price_cents, promo_price_cents, promo_rule_id, in_stock)` with `max_bridge_gap_days = 2`.
2. **PostgreSQL Quarterly Partitioning, HOT Updates & BRIN Indexing**:
   - `price_spans` is declaratively partitioned by `RANGE (valid_from)` into quarterly partitions (`fillfactor = 85` on hot quarters for Heap-Only Tuple updates; `fillfactor = 100` + `VACUUM FREEZE` on closed quarters; `BRIN` index on `(valid_from, last_seen)`).
3. **64-Bit Centavo Precision (`BIGINT`) & Monetary Regimes**:
   - Nominal prices stored as `BIGINT` centavos alongside `monetary_regimes` for currency reform resilience.
4. **Pre-Aggregated Rollups + Cold `chain_city_spans` Compaction Safety Valve**:
   - Sub-second analytical queries hit `daily_geo_variety_rollups`, `monthly_geo_variety_rollups`, and `price_index_series`, while `chain_city_spans` provides an optional $5\times$–$8\times$ cold-partition compaction valve if disk footprint ever approaches `380 GB`.

---

## 3. Two-Stage National & Cross-Government CPI Methodology

1. **Stage 1 — Elementary Cell Index (Jevons Geometric Mean)**:
   - Unit prices (`ARS/kg`, `ARS/L`, `ARS/u`) within each `(date, geo, generic_variety, channel)` cell aggregate via geometric mean alongside IQR dispersion, promo wedge %, and stockout rate.
2. **Stage 2 — Chained Regional & National Index (Laspeyres / Young)**:
   - Links overlapping varieties month-over-month across **24 Provinces $\rightarrow$ 6 INDEC Regions $\rightarrow$ Country (`AR`)** using configurable ENGHo expenditure or population weights.
3. **Cross-Government Period Comparator**:
   - Computes cumulative inflation, **Compound Monthly Growth Rate (CMGR)** across unequal-length terms, annualized rate, monthly volatility ($\sigma$), `INDEC_IPC` gap, and `RIPTE` real wage purchasing power.
