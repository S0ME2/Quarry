"""Offline PostgreSQL access-path benchmark in a disposable test schema.

Mirrors queried columns and existing indexes, not domain integrity fixtures. Measures
repeated query plans, not end-to-end API latency. Never writes development tables.
Run: uv run python -m tools.database_workload --rows 100000
"""

import argparse
import json
import os
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, text
from sqlalchemy.schema import CreateSchema, DropSchema

from tender_system.core.environment import load_environment
from tender_system.persistence.storage import (
    build_engine,
    database_url_from_environment,
    validate_test_database,
)


def fixture(connection: Connection, rows: int) -> None:
    """Generate deterministic, modest payloads with 100 sources and 20 jobs/tender."""
    statements = [
        """CREATE TABLE tenders (
            id uuid PRIMARY KEY, source_id text NOT NULL, external_tender_id text,
            last_seen_at timestamptz NOT NULL, title text, description text, buyer text,
            status text, current_version_id uuid, latest_observation_id uuid)""",
        "CREATE INDEX ix_tenders_source ON tenders (source_id)",
        """CREATE UNIQUE INDEX uq_tenders_external_identity
            ON tenders (source_id, external_tender_id) WHERE external_tender_id IS NOT NULL""",
        """INSERT INTO tenders SELECT md5(i::text)::uuid, 'source-' || (i % 100), i::text,
            '2026-01-01'::timestamptz + (i % 10000) * interval '1 minute',
            CASE WHEN i % 100 = 0 THEN 'Network router' ELSE 'Office supplies' END,
            repeat('Procurement specification ', 12), 'Buyer ' || (i % 500), 'active',
            md5(('version-' || i)::text)::uuid, md5(('observation-' || i)::text)::uuid
            FROM generate_series(1, :rows) AS g(i)""",
        """CREATE TABLE tender_versions (id uuid PRIMARY KEY, tender_id uuid NOT NULL,
            observed_at timestamptz, changed_fields jsonb, UNIQUE(tender_id, id))""",
        """INSERT INTO tender_versions SELECT md5((i || '-' || v)::text)::uuid,
            md5(i::text)::uuid, '2026-01-01'::timestamptz + v * interval '1 day',
            '{"title": "Network router"}'::jsonb
            FROM generate_series(1, :rows) AS g(i), generate_series(1, 3) AS h(v)""",
        """CREATE TABLE match_jobs (id uuid PRIMARY KEY, tender_id uuid NOT NULL,
            tender_version_id uuid NOT NULL, campaign_id uuid NOT NULL,
            criteria_version_id uuid NOT NULL, matcher_implementation text NOT NULL,
            matcher_version text NOT NULL, settings_hash text NOT NULL, status text NOT NULL,
            attempts integer, max_attempts integer, created_at timestamptz,
            available_at timestamptz, lease_expires_at timestamptz, matcher_config jsonb,
            UNIQUE(tender_id, tender_version_id, campaign_id, criteria_version_id,
                matcher_implementation, matcher_version, settings_hash))""",
        """CREATE INDEX ix_match_jobs_pending ON match_jobs (available_at, created_at)
            WHERE status = 'pending'""",
        """CREATE INDEX ix_match_jobs_expired ON match_jobs (lease_expires_at)
            WHERE status = 'running'""",
        "CREATE INDEX ix_match_jobs_campaign ON match_jobs (campaign_id, criteria_version_id)",
        """INSERT INTO match_jobs SELECT md5(('job-' || i)::text)::uuid,
            md5(((i - 1) / 20 + 1)::text)::uuid, md5(('version-' || i)::text)::uuid,
            md5((i % 10)::text)::uuid, md5('criteria')::uuid, 'rules', '1', 'settings',
            CASE WHEN i % 1000 = 0 THEN 'pending'
                WHEN i % 1001 = 0 THEN 'running' ELSE 'done' END,
            0, 3, '2026-01-01'::timestamptz + i * interval '1 second',
            '2026-01-01'::timestamptz, '2026-01-02'::timestamptz, '{}'::jsonb
            FROM generate_series(1, :rows * 2) AS g(i)""",
        """CREATE TABLE match_results (id uuid PRIMARY KEY, campaign_id uuid,
            tender_id uuid, created_at timestamptz, outcome jsonb)""",
        """CREATE INDEX ix_match_results_campaign
            ON match_results (campaign_id, created_at)""",
        """INSERT INTO match_results SELECT id, campaign_id, tender_id, created_at,
            '{"result":"match", "explanation":"Fixture evidence"}'::jsonb FROM match_jobs""",
        "ANALYZE tenders",
        "ANALYZE tender_versions",
        "ANALYZE match_jobs",
        "ANALYZE match_results",
    ]
    for statement in statements:
        connection.execute(text(statement), {"rows": rows})


QUERIES = {
    "tender_page": "SELECT id FROM tenders ORDER BY last_seen_at DESC, id LIMIT 50",
    "tender_count": "SELECT count(*) FROM tenders",
    "source_tender_page": """SELECT id FROM tenders WHERE source_id = 'source-7'
        ORDER BY last_seen_at DESC, id LIMIT 50""",
    "source_candidates": """SELECT * FROM tenders WHERE source_id = 'source-7'
        AND id > md5('500')::uuid ORDER BY id LIMIT 100""",
    "tender_search": """SELECT id FROM tenders WHERE title ILIKE '%router%'
        OR description ILIKE '%router%' OR buyer ILIKE '%router%'
        ORDER BY last_seen_at DESC, id LIMIT 50""",
    "tender_search_count": """SELECT count(*) FROM tenders WHERE title ILIKE '%router%'
        OR description ILIKE '%router%' OR buyer ILIKE '%router%'""",
    "buyer_count": "SELECT count(*) FROM tenders WHERE buyer ILIKE '%Buyer 7%'",
    "buyer_page": """SELECT id FROM tenders WHERE buyer ILIKE '%Buyer 7%'
        ORDER BY last_seen_at DESC, id LIMIT 50""",
    "tender_history": """SELECT * FROM tender_versions
        WHERE tender_id = md5('500')::uuid ORDER BY observed_at, id""",
    "matching_cache": """SELECT * FROM match_jobs WHERE tender_id = md5('500')::uuid
        AND campaign_id = md5('0')::uuid AND criteria_version_id = md5('criteria')::uuid
        AND matcher_implementation = 'rules' AND matcher_version = '1'
        AND settings_hash = 'settings' ORDER BY created_at, id""",
    "claim_candidates": """SELECT * FROM match_jobs WHERE attempts < max_attempts AND
        ((status = 'pending' AND available_at <= '2026-02-01'::timestamptz)
         OR (status = 'running' AND lease_expires_at <= '2026-02-01'::timestamptz))
        ORDER BY created_at, id LIMIT 10 FOR UPDATE SKIP LOCKED""",
    "campaign_result_page": """SELECT * FROM match_results
        WHERE campaign_id = md5('0')::uuid ORDER BY created_at DESC, id DESC LIMIT 50""",
}

PROPOSED = {
    "ix_tenders_recent": "CREATE INDEX ix_tenders_recent ON tenders (last_seen_at DESC, id)",
    "ix_tenders_source_recent": """CREATE INDEX ix_tenders_source_recent
        ON tenders (source_id, last_seen_at DESC, id)""",
}


def measure(connection: Connection, repeats: int) -> dict[str, Any]:
    measurements = {}
    for name, query in QUERIES.items():
        plans = [
            connection.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query))[0]
            for _ in range(repeats)
        ]
        measurements[name] = {
            "median_execution_ms": median(plan["Execution Time"] for plan in plans),
            "plan": plans[-1],
            "sql": query,
        }
    return measurements


def run(rows: int = 100000, repeats: int = 5, trigram: bool = False) -> dict[str, Any]:
    if not 1000 <= rows <= 1000000 or not 1 <= repeats <= 20:
        raise ValueError("rows must be 1000..1000000; repeats must be 1..20")
    load_environment()
    url = database_url_from_environment("TEST_DATABASE_URL")
    validate_test_database(url.get_secret_value(), os.environ.get("DATABASE_URL"))
    engine = build_engine(url)
    schema = f"workload_{uuid4().hex}"
    try:
        # DDL, fixture, plans and schema removal share a transaction. An error rolls
        # back the schema itself; no persistent fixture or production writes remain.
        with engine.begin() as connection:
            actual = connection.scalar(text("SELECT current_database()"))
            if actual != engine.url.database or not actual or "test" not in actual.lower():
                raise ValueError("Connected database is not the selected test database")
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            fixture(connection, rows)
            report = {
                "server": connection.scalar(text("SHOW server_version")),
                "fixture": {"tenders": rows, "versions": rows * 3, "jobs": rows * 2},
                "repeats": repeats,
                "baseline": measure(connection, repeats),
            }
            for statement in PROPOSED.values():
                connection.execute(text(statement))
            # The composite index replaces this leading-prefix-only index.
            connection.execute(text("DROP INDEX ix_tenders_source"))
            report["indexed"] = measure(connection, repeats)
            report["proposed_indexes"] = PROPOSED
            if trigram:
                started = perf_counter()
                connection.execute(
                    text(f'CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA "{schema}"')
                )
                extension_schema = connection.scalar(
                    text(
                        "SELECT n.nspname FROM pg_extension e "
                        "JOIN pg_namespace n ON n.oid = e.extnamespace "
                        "WHERE e.extname = 'pg_trgm'"
                    )
                )
                if not isinstance(extension_schema, str):
                    raise ValueError("pg_trgm installation is unavailable")
                opclass = connection.dialect.identifier_preparer.quote(extension_schema)
                for column in ("title", "description", "buyer"):
                    connection.execute(
                        text(
                            f"CREATE INDEX ix_tenders_{column}_trgm ON tenders "
                            f"USING gin ({column} {opclass}.gin_trgm_ops)"
                        )
                    )
                report["trigram_build_seconds"] = perf_counter() - started
                report["trigram_index_bytes"] = connection.scalar(
                    text("""
                        SELECT sum(pg_relation_size(indexrelid))::bigint FROM pg_index
                    JOIN pg_class ON pg_class.oid = indexrelid
                    JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
                    WHERE nspname = :schema AND relname LIKE 'ix_tenders_%_trgm'
                """),
                    {"schema": schema},
                )
                report["trigram"] = measure(connection, repeats)
            connection.execute(DropSchema(schema, cascade=True))
            return report
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--trigram", action="store_true", help="Measure pg_trgm GIN search indexes")
    args = parser.parse_args()
    print(json.dumps(run(args.rows, args.repeats, args.trigram), indent=2))


if __name__ == "__main__":
    main()
