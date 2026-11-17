"""Clean Quarry baseline: fresh databases only.

Legacy 0001_sources..0013_matching_spend installations must be backed up and
imported into a fresh database with tools/database_rebuild.py. No in-place
upgrade path exists. DDL is frozen here; application metadata cannot change it.
"""

from alembic import op

revision = "0001_quarry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_extension
        WHERE extname = 'pg_trgm' AND extnamespace != 'public'::regnamespace
    ) THEN
        RAISE EXCEPTION 'Quarry requires pg_trgm in public; relocate the existing extension '
            'to public before creating the fresh Quarry database';
    END IF;
END $$
    """)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    op.execute("""
CREATE TABLE campaigns (
	id UUID NOT NULL,
	active BOOLEAN NOT NULL,
	current_criteria_version_id UUID NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_campaign_deleted_inactive CHECK (deleted_at IS NULL OR NOT active)
)
    """)
    op.execute("""
CREATE TABLE operations (
	id UUID NOT NULL,
	kind TEXT NOT NULL,
	source_id TEXT,
	status TEXT NOT NULL,
	payload JSONB NOT NULL,
	result JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	error_code TEXT,
	PRIMARY KEY (id),
        CONSTRAINT ck_operation_status CHECK (status IN ('queued', 'running', 'completed',
    'failed', 'awaiting_approval', 'activated', 'rejected', 'escalated'))
)
    """)
    op.execute("""
CREATE INDEX ix_operations_queue ON operations (status, created_at)
    """)
    op.execute("""
CREATE TABLE settings (
	key TEXT NOT NULL,
	value JSONB NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (key)
)
    """)
    op.execute("""
CREATE TABLE sources (
	id TEXT NOT NULL,
	name TEXT NOT NULL,
	has_stable_external_id BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_sources_id_nonblank CHECK (length(trim(id)) > 0),
	CONSTRAINT ck_sources_name_nonblank CHECK (length(trim(name)) > 0),
	CONSTRAINT uq_sources_identity_policy UNIQUE (id, has_stable_external_id)
)
    """)
    op.execute("""
CREATE TABLE campaign_criteria (
	id UUID NOT NULL,
	campaign_id UUID NOT NULL,
	previous_version_id UUID,
	number INTEGER NOT NULL,
	criteria JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_criteria_campaign FOREIGN KEY(campaign_id) REFERENCES campaigns (id),
	CONSTRAINT uq_criteria_owner UNIQUE (campaign_id, id),
	CONSTRAINT uq_criteria_number UNIQUE (campaign_id, number),
	CONSTRAINT uq_criteria_no_fork UNIQUE (previous_version_id),
        CONSTRAINT fk_criteria_previous_owner FOREIGN KEY(campaign_id, previous_version_id)
    REFERENCES campaign_criteria (campaign_id, id),
	CONSTRAINT ck_criteria_number CHECK (number > 0),
	CONSTRAINT ck_criteria_no_self CHECK (previous_version_id IS NULL OR previous_version_id != id)
)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_criteria_initial ON campaign_criteria (campaign_id) WHERE
    previous_version_id IS NULL
    """)
    op.execute("""
CREATE TABLE recipe_revisions (
	source_id TEXT NOT NULL,
	version TEXT NOT NULL,
	revision JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (source_id, version),
	CONSTRAINT fk_recipe_revision_source FOREIGN KEY(source_id) REFERENCES sources (id)
)
    """)
    op.execute("""
CREATE TABLE source_runs (
	id UUID NOT NULL,
	source_id TEXT NOT NULL,
	status TEXT NOT NULL,
	accepted_sequence BIGINT,
	receipt JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_source_runs_source FOREIGN KEY(source_id) REFERENCES sources (id),
	CONSTRAINT uq_source_runs_sequence UNIQUE (source_id, accepted_sequence),
	CONSTRAINT ck_source_runs_status CHECK (status IN ('accepted', 'rejected', 'failed')),
        CONSTRAINT ck_source_runs_sequence CHECK ((status = 'accepted' AND accepted_sequence IS
    NOT NULL AND accepted_sequence > 0) OR (status <> 'accepted' AND accepted_sequence IS NULL))
)
    """)
    op.execute("""
CREATE INDEX ix_source_runs_source_id ON source_runs (source_id)
    """)
    op.execute("""
CREATE TABLE source_states (
	source_id TEXT NOT NULL,
	accepted_sequence BIGINT NOT NULL,
	checkpoint JSONB,
	last_full_count BIGINT,
	last_good_evidence_reference TEXT,
	last_finished_at TIMESTAMP WITH TIME ZONE,
	evidence_state JSONB,
	PRIMARY KEY (source_id),
	CONSTRAINT fk_source_states_source FOREIGN KEY(source_id) REFERENCES sources (id),
	CONSTRAINT ck_source_states_sequence CHECK (accepted_sequence >= 0),
	CONSTRAINT ck_source_states_count CHECK (last_full_count >= 0)
)
    """)
    op.execute("""
CREATE TABLE tenders (
	id UUID NOT NULL,
	source_id TEXT NOT NULL,
	external_tender_id TEXT,
	source_has_stable_external_id BOOLEAN NOT NULL,
	fallback_kind TEXT,
	fallback_key TEXT,
	source_url TEXT,
	title TEXT,
	description TEXT,
	buyer TEXT,
	location TEXT,
	published_at TIMESTAMP WITH TIME ZONE,
	deadline TIMESTAMP WITH TIME ZONE,
	value NUMERIC,
	currency TEXT,
	status TEXT,
	document_urls JSONB NOT NULL,
	product_items JSONB,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	current_version_id UUID NOT NULL,
	latest_observation_id UUID NOT NULL,
	missing_successful_runs INTEGER NOT NULL,
	last_accounted_run_sequence BIGINT,
	PRIMARY KEY (id),
        CONSTRAINT fk_tenders_source_policy FOREIGN KEY(source_id,
    source_has_stable_external_id) REFERENCES sources (id, has_stable_external_id),
        CONSTRAINT ck_tenders_identity CHECK ((source_has_stable_external_id AND
    external_tender_id IS NOT NULL AND length(trim(external_tender_id)) > 0 AND fallback_kind IS
    NULL AND fallback_key IS NULL) OR (external_tender_id IS NULL AND NOT
    source_has_stable_external_id AND fallback_kind IS NOT NULL AND fallback_kind IN ('url',
    'fingerprint') AND fallback_key IS NOT NULL AND length(trim(fallback_key)) > 0)),
        CONSTRAINT ck_tenders_value CHECK (value IS NULL OR (value >= 0 AND value !=
    'NaN'::numeric AND value != 'Infinity'::numeric)),
	CONSTRAINT ck_tenders_seen_order CHECK (last_seen_at >= first_seen_at),
	CONSTRAINT ck_tenders_missing CHECK (missing_successful_runs >= 0),
        CONSTRAINT ck_tenders_presence_sequence CHECK (last_accounted_run_sequence IS NULL OR
    last_accounted_run_sequence >= 0)
)
    """)
    op.execute("""
CREATE INDEX ix_tenders_recent ON tenders (last_seen_at DESC, id)
    """)
    op.execute("""
CREATE INDEX ix_tenders_source_recent ON tenders (source_id, last_seen_at DESC, id)
    """)
    op.execute("""
CREATE INDEX ix_tenders_title_trgm ON tenders USING gin (title public.gin_trgm_ops)
    """)
    op.execute("""
CREATE INDEX ix_tenders_description_trgm ON tenders USING gin (description public.gin_trgm_ops)
    """)
    op.execute("""
CREATE INDEX ix_tenders_buyer_trgm ON tenders USING gin (buyer public.gin_trgm_ops)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_tenders_external_identity ON tenders (source_id, external_tender_id)
    WHERE external_tender_id IS NOT NULL
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_tenders_fallback_identity ON tenders (source_id, fallback_kind,
    fallback_key) WHERE external_tender_id IS NULL
    """)
    op.execute("""
CREATE TABLE active_recipes (
	source_id TEXT NOT NULL,
	version TEXT NOT NULL,
	PRIMARY KEY (source_id),
        CONSTRAINT fk_active_recipe_revision FOREIGN KEY(source_id, version) REFERENCES
    recipe_revisions (source_id, version)
)
    """)
    op.execute("""
CREATE TABLE tender_versions (
	id UUID NOT NULL,
	tender_id UUID NOT NULL,
	previous_version_id UUID,
	changed_fields JSONB NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	evidence_reference TEXT NOT NULL,
	observation_id UUID NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_versions_tender FOREIGN KEY(tender_id) REFERENCES tenders (id),
	CONSTRAINT uq_versions_owner UNIQUE (tender_id, id),
        CONSTRAINT fk_versions_previous_owner FOREIGN KEY(tender_id, previous_version_id)
    REFERENCES tender_versions (tender_id, id),
	CONSTRAINT uq_versions_no_fork UNIQUE (previous_version_id),
	CONSTRAINT uq_versions_observation UNIQUE (observation_id),
        CONSTRAINT ck_versions_no_self_link CHECK (previous_version_id IS NULL OR
    previous_version_id != id),
        CONSTRAINT ck_versions_nonempty_diff CHECK (jsonb_typeof(changed_fields) = 'object' AND
    changed_fields != '{}'::jsonb)
)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_versions_initial ON tender_versions (tender_id) WHERE previous_version_id
    IS NULL
    """)
    op.execute("""
CREATE TABLE match_jobs (
	id UUID NOT NULL,
	tender_id UUID NOT NULL,
	tender_version_id UUID NOT NULL,
	campaign_id UUID NOT NULL,
	criteria_version_id UUID NOT NULL,
	matcher_implementation TEXT NOT NULL,
	matcher_version TEXT NOT NULL,
	matcher_config JSONB NOT NULL,
	settings_hash TEXT NOT NULL,
	status TEXT NOT NULL,
	attempts INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	last_error_code TEXT,
	lease_token UUID,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
        CONSTRAINT fk_job_tender_version FOREIGN KEY(tender_id, tender_version_id) REFERENCES
    tender_versions (tender_id, id),
        CONSTRAINT fk_job_criteria_owner FOREIGN KEY(campaign_id, criteria_version_id)
    REFERENCES campaign_criteria (campaign_id, id),
        CONSTRAINT uq_match_job_work UNIQUE (tender_id, tender_version_id, campaign_id,
    criteria_version_id, matcher_implementation, matcher_version, settings_hash),
        CONSTRAINT uq_match_job_result_owner UNIQUE (id, tender_id, tender_version_id,
    campaign_id, criteria_version_id, matcher_implementation, matcher_version, settings_hash),
	CONSTRAINT ck_match_job_status CHECK (status IN ('pending', 'running', 'done', 'failed')),
	CONSTRAINT ck_match_job_attempts CHECK (attempts >= 0 AND max_attempts > 0),
        CONSTRAINT ck_match_job_lease CHECK ((status = 'running' AND lease_token IS NOT NULL AND
    lease_expires_at IS NOT NULL) OR (status != 'running' AND lease_token IS NULL AND
    lease_expires_at IS NULL))
)
    """)
    op.execute("""
CREATE INDEX ix_match_jobs_campaign ON match_jobs (campaign_id, criteria_version_id)
    """)
    op.execute("""
CREATE INDEX ix_match_jobs_expired ON match_jobs (lease_expires_at) WHERE status = 'running'
    """)
    op.execute("""
CREATE INDEX ix_match_jobs_pending ON match_jobs (available_at, created_at) WHERE status =
    'pending'
    """)
    op.execute("""
CREATE TABLE tender_observations (
	id UUID NOT NULL,
	tender_id UUID NOT NULL,
	version_id UUID NOT NULL,
	payload_hash TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	evidence_reference TEXT NOT NULL,
	source_run_id UUID,
	raw_data JSONB NOT NULL,
	quality_issues JSONB NOT NULL,
	normalization_overrides JSONB NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_observations_owner UNIQUE (tender_id, id),
        CONSTRAINT fk_observations_version_owner FOREIGN KEY(tender_id, version_id) REFERENCES
    tender_versions (tender_id, id)
)
    """)
    op.execute("""
CREATE INDEX ix_observations_tender ON tender_observations (tender_id)
    """)
    op.execute("""
CREATE TABLE match_results (
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	tender_id UUID NOT NULL,
	tender_version_id UUID NOT NULL,
	campaign_id UUID NOT NULL,
	criteria_version_id UUID NOT NULL,
	matcher_implementation TEXT NOT NULL,
	matcher_version TEXT NOT NULL,
	settings_hash TEXT NOT NULL,
	outcome JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completion_token UUID NOT NULL,
	PRIMARY KEY (id),
        CONSTRAINT fk_match_result_work FOREIGN KEY(job_id, tender_id, tender_version_id,
    campaign_id, criteria_version_id, matcher_implementation, matcher_version, settings_hash)
    REFERENCES match_jobs (id, tender_id, tender_version_id, campaign_id, criteria_version_id,
    matcher_implementation, matcher_version, settings_hash),
	CONSTRAINT uq_match_result_job UNIQUE (job_id)
)
    """)
    op.execute("""
CREATE INDEX ix_match_results_campaign ON match_results (campaign_id, created_at)
    """)
    op.execute("""
CREATE INDEX ix_match_results_tender ON match_results (tender_id, created_at)
    """)
    op.execute("""
CREATE TABLE matching_spend (
	job_id UUID NOT NULL,
	campaign_id UUID NOT NULL,
	run_id UUID NOT NULL,
	reserved_usd NUMERIC NOT NULL,
	accounted_usd NUMERIC NOT NULL,
	input_tokens BIGINT,
	output_tokens BIGINT,
	state TEXT NOT NULL,
	PRIMARY KEY (job_id),
	CHECK (reserved_usd >= 0 AND accounted_usd >= 0),
	FOREIGN KEY(job_id) REFERENCES match_jobs (id),
	FOREIGN KEY(campaign_id) REFERENCES campaigns (id)
)
    """)
    op.execute("""
CREATE INDEX ix_matching_spend_campaign ON matching_spend (campaign_id)
    """)
    op.execute("""
CREATE INDEX ix_matching_spend_run_id ON matching_spend (run_id)
    """)
    op.execute("""
ALTER TABLE campaigns ADD CONSTRAINT fk_campaign_current_criteria FOREIGN KEY(id,
    current_criteria_version_id) REFERENCES campaign_criteria (campaign_id, id) DEFERRABLE
    INITIALLY DEFERRED
    """)
    op.execute("""
ALTER TABLE tenders ADD CONSTRAINT fk_tenders_current_version FOREIGN KEY(id,
    current_version_id) REFERENCES tender_versions (tender_id, id) DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
ALTER TABLE tenders ADD CONSTRAINT fk_tenders_latest_observation FOREIGN KEY(id,
    latest_observation_id) REFERENCES tender_observations (tender_id, id) DEFERRABLE INITIALLY
    DEFERRED
    """)
    op.execute("""
ALTER TABLE tender_versions ADD CONSTRAINT fk_versions_observation FOREIGN KEY(tender_id,
    observation_id) REFERENCES tender_observations (tender_id, id) DEFERRABLE INITIALLY DEFERRED
    """)
    op.execute("""
CREATE FUNCTION reject_audit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Quarry history and receipts are append-only';
END;
$$
    """)
    for table in (
        "tender_versions",
        "tender_observations",
        "campaign_criteria",
        "match_results",
        "recipe_revisions",
        "source_runs",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_audit BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER no_audit_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_mutation()"
        )
    op.execute("""
CREATE FUNCTION protect_match_job_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status', 'attempts', 'available_at', 'started_at',
        'finished_at', 'last_error_code', 'lease_token', 'lease_expires_at'])
        IS DISTINCT FROM
        (to_jsonb(OLD) - ARRAY['status', 'attempts', 'available_at', 'started_at',
        'finished_at', 'last_error_code', 'lease_token', 'lease_expires_at']) THEN
        RAISE EXCEPTION 'Matching job inputs are immutable';
    END IF;
    RETURN NEW;
END;
$$
    """)
    op.execute("""
CREATE TRIGGER immutable_match_job_inputs BEFORE UPDATE ON match_jobs FOR EACH ROW EXECUTE
    FUNCTION protect_match_job_identity()
    """)


def downgrade() -> None:
    op.drop_constraint("fk_campaign_current_criteria", "campaigns", type_="foreignkey")
    op.drop_constraint("fk_tenders_current_version", "tenders", type_="foreignkey")
    op.drop_constraint("fk_tenders_latest_observation", "tenders", type_="foreignkey")
    op.drop_constraint("fk_versions_observation", "tender_versions", type_="foreignkey")
    op.drop_table("matching_spend")
    op.drop_table("match_results")
    op.drop_table("tender_observations")
    op.drop_table("match_jobs")
    op.drop_table("tender_versions")
    op.drop_table("active_recipes")
    op.drop_table("tenders")
    op.drop_table("source_states")
    op.drop_table("source_runs")
    op.drop_table("recipe_revisions")
    op.drop_table("campaign_criteria")
    op.drop_table("sources")
    op.drop_table("settings")
    op.drop_table("operations")
    op.drop_table("campaigns")
    op.execute("DROP FUNCTION protect_match_job_identity()")
    op.execute("DROP FUNCTION reject_audit_mutation()")
