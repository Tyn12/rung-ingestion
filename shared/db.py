"""Postgres connection and idempotent upsert helpers."""
from __future__ import annotations
import os
from contextlib import contextmanager
from typing import Iterable
import psycopg2
import psycopg2.extras

from .models import CompensationObservation


@contextmanager
def get_conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set; copy .env.example to .env and fill in.")
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


UPSERT_SQL = """
INSERT INTO compensation_observations (
    source_id, source_reference, occupation_code, location_code, company_ref,
    observation_type, value_amount, value_min, value_max, percentile,
    period, normalized_annual_amount, normalization_method_version, currency,
    experience_band, contract_type, sample_size, total_comp_annual,
    observed_at, observed_year, source_payload
) VALUES (
    %(source_id)s, %(source_reference)s, %(occupation_code)s, %(location_code)s, %(company_ref)s,
    %(observation_type)s, %(value_amount)s, %(value_min)s, %(value_max)s, %(percentile)s,
    %(period)s, %(normalized_annual_amount)s, %(normalization_method_version)s, %(currency)s,
    %(experience_band)s, %(contract_type)s, %(sample_size)s, %(total_comp_annual)s,
    %(observed_at)s, %(observed_year)s, %(source_payload)s
)
ON CONFLICT (source_id, source_reference, observed_year) DO UPDATE SET
    value_amount             = EXCLUDED.value_amount,
    value_min                = EXCLUDED.value_min,
    value_max                = EXCLUDED.value_max,
    percentile               = EXCLUDED.percentile,
    normalized_annual_amount = EXCLUDED.normalized_annual_amount,
    experience_band          = EXCLUDED.experience_band,
    contract_type            = EXCLUDED.contract_type,
    sample_size              = EXCLUDED.sample_size,
    total_comp_annual        = EXCLUDED.total_comp_annual,
    source_payload           = EXCLUDED.source_payload,
    updated_at               = NOW();
"""


def bulk_upsert(observations: Iterable[CompensationObservation]) -> int:
    """Upsert a batch of observations, returning the number processed."""
    rows = [obs.to_dict() for obs in observations]
    if not rows:
        return 0
    # psycopg2 doesn't serialize dicts to JSONB automatically — wrap.
    for row in rows:
        row["source_payload"] = psycopg2.extras.Json(row.get("source_payload") or {})
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT_SQL, rows, page_size=500)
    return len(rows)
