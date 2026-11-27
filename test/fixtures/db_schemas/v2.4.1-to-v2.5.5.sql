-- Schema from v2.4.1-to-v2.5.5, commit e2f2978c26c1b50cee2db90b524e7656ef64f46f; DDL only, no rows.
CREATE INDEX idx_memory_rel_lookup ON memory_relationships (scope, scope_id, source_key);

CREATE INDEX idx_memory_scope ON memory_metadata (scope, scope_id);

CREATE INDEX idx_memory_type ON memory_metadata (memory_type);

CREATE INDEX idx_memory_updated ON memory_metadata (updated_at);

CREATE INDEX idx_outcome_agent ON workflow_outcomes (agent_profile, created_at);

CREATE INDEX idx_outcome_session ON workflow_outcomes (session_name, created_at);

CREATE INDEX idx_workflow_run_started_at ON workflow_run (started_at);

CREATE INDEX idx_workflow_run_state ON workflow_run (state);

CREATE INDEX ix_decision_records_created_at ON decision_records (created_at);

CREATE INDEX ix_decision_records_point_created_at ON decision_records (point, created_at);

CREATE INDEX ix_decision_records_terminal_id ON decision_records (terminal_id);

CREATE INDEX ix_delegation_edges_caller_id ON delegation_edges (caller_id);

CREATE INDEX ix_delegation_edges_state ON delegation_edges (state);

CREATE INDEX ix_delegation_edges_worker_id ON delegation_edges (worker_id);

CREATE INDEX ix_ephemeral_agents_launched_terminal_id ON ephemeral_agents (launched_terminal_id);

CREATE INDEX ix_kept_terminals_session_name ON kept_terminals (session_name);

CREATE INDEX ix_launch_records_created_at ON launch_records (created_at);

CREATE INDEX ix_launch_records_ended_at ON launch_records (ended_at);

CREATE INDEX ix_launch_records_terminal_id ON launch_records (terminal_id);

CREATE INDEX ix_launch_status_events_terminal_created ON launch_status_events (terminal_id, created_at);

CREATE INDEX ix_project_aliases_project_id ON project_aliases (project_id);

CREATE UNIQUE INDEX uq_memory_key_scope_null ON memory_metadata (key, scope, source_kind) WHERE scope_id IS NULL;

CREATE UNIQUE INDEX uq_memory_rel ON memory_relationships (scope, scope_id, source_key, target_key, type, origin);

CREATE TABLE decision_records (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	contract_version INTEGER NOT NULL, 
	point VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	decider VARCHAR, 
	decider_version VARCHAR, 
	kind VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	agent_profile VARCHAR, 
	fallback_value VARCHAR, 
	fallback_source VARCHAR NOT NULL, 
	answer VARCHAR, 
	probabilities TEXT, 
	confidence FLOAT, 
	candidate_value VARCHAR, 
	candidate_model VARCHAR, 
	applied_value VARCHAR, 
	outcome VARCHAR NOT NULL, 
	reason VARCHAR, 
	launched_model VARCHAR, 
	model_honored BOOLEAN, 
	latency_ms FLOAT, 
	decision_status VARCHAR NOT NULL, 
	launch_status VARCHAR NOT NULL, 
	terminal_id VARCHAR, 
	message_hash VARCHAR, 
	hash_key_id VARCHAR, 
	message_bytes INTEGER
, target_kind TEXT, options TEXT, fallback_enforcement TEXT, applied_enforcement TEXT, match_rule TEXT, match_needs TEXT, match_covered TEXT, match_tiebreak TEXT, match_from TEXT, match_pick TEXT, catalog_version INTEGER, catalog_hash TEXT);

CREATE TABLE delegation_edges (
	id INTEGER NOT NULL, 
	caller_id VARCHAR NOT NULL, 
	worker_id VARCHAR NOT NULL, 
	source VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	reason VARCHAR NOT NULL, 
	message_id INTEGER, 
	created_at DATETIME, 
	delivered_at DATETIME, 
	closed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE ephemeral_agents (
	name VARCHAR NOT NULL, 
	owner_kind VARCHAR NOT NULL, 
	owner_id VARCHAR NOT NULL, 
	session_name VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	claim_id VARCHAR, 
	idempotency_key VARCHAR, 
	claim_expires_at DATETIME, 
	launched_terminal_id VARCHAR, 
	bound_at DATETIME, 
	model_tier VARCHAR, 
	effort VARCHAR, 
	provider VARCHAR NOT NULL, 
	effective_tools TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	gc_reason VARCHAR, 
	spec_sha256 VARCHAR NOT NULL, 
	profile_sha256 VARCHAR NOT NULL, 
	audit_path VARCHAR NOT NULL, 
	PRIMARY KEY (name)
);

CREATE TABLE flows (
	name VARCHAR NOT NULL, 
	file_path VARCHAR NOT NULL, 
	schedule VARCHAR NOT NULL, 
	agent_profile VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	script VARCHAR, 
	last_run DATETIME, 
	next_run DATETIME, 
	enabled BOOLEAN, 
	PRIMARY KEY (name)
);

CREATE TABLE handoff_results (
	job_id VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	terminal_id VARCHAR, 
	last_message TEXT, 
	error_message TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (job_id)
);

CREATE TABLE idempotency_keys (
	"key" VARCHAR NOT NULL, 
	terminal_id VARCHAR NOT NULL, 
	request_fingerprint VARCHAR NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY ("key")
);

CREATE TABLE inbox (
	id INTEGER NOT NULL, 
	sender_id VARCHAR NOT NULL, 
	receiver_id VARCHAR NOT NULL, 
	message VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE kept_terminals (
	terminal_id VARCHAR NOT NULL, 
	session_name VARCHAR NOT NULL, 
	kept_at DATETIME NOT NULL, 
	kept_until DATETIME NOT NULL, 
	reason VARCHAR NOT NULL, 
	PRIMARY KEY (terminal_id)
);

CREATE TABLE launch_records (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	terminal_id VARCHAR NOT NULL, 
	session_name VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	target_name VARCHAR, 
	target_kind VARCHAR, 
	launched_model VARCHAR, 
	model_honored BOOLEAN, 
	provider_session_id VARCHAR, 
	record_ids TEXT, 
	caller_id VARCHAR, 
	outcome VARCHAR, 
	outcome_recorded_at DATETIME, 
	outcome_reporter_id VARCHAR, 
	created_at DATETIME NOT NULL, 
	ended_at DATETIME, 
	end_reason VARCHAR
);

CREATE TABLE launch_status_events (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	terminal_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL
);

CREATE TABLE "memory_metadata" (
                    id VARCHAR NOT NULL PRIMARY KEY,
                    key VARCHAR NOT NULL,
                    memory_type VARCHAR NOT NULL,
                    scope VARCHAR NOT NULL,
                    scope_id VARCHAR,
                    source_kind VARCHAR NOT NULL DEFAULT 'native',
                    file_path VARCHAR NOT NULL,
                    tags VARCHAR NOT NULL,
                    source_provider VARCHAR,
                    source_terminal_id VARCHAR,
                    token_estimate INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at DATETIME,
                    last_compiled_at DATETIME,
                    related_keys TEXT,
                    CONSTRAINT uq_memory_key_scope UNIQUE (key, scope, scope_id, source_kind)
                );

CREATE TABLE memory_relationships (
	id VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	scope_id VARCHAR NOT NULL, 
	source_key VARCHAR NOT NULL, 
	target_key VARCHAR NOT NULL, 
	type VARCHAR NOT NULL, 
	origin VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	confidence FLOAT, 
	rank INTEGER, 
	attributes_json TEXT, 
	source_updated_at DATETIME, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_memory_rel UNIQUE (scope, scope_id, source_key, target_key, type, origin), 
	CONSTRAINT ck_memory_rel_confidence_range CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)), 
	CONSTRAINT ck_memory_rel_attributes_size CHECK (attributes_json IS NULL OR length(attributes_json) <= 2048)
);

CREATE TABLE project_aliases (
	alias VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (alias)
);

CREATE TABLE session_incarnations (
	session_name VARCHAR NOT NULL, 
	incarnation_id VARCHAR NOT NULL, 
	PRIMARY KEY (session_name)
);

CREATE TABLE terminals (
	id VARCHAR NOT NULL, 
	tmux_session VARCHAR NOT NULL, 
	tmux_window VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	agent_profile VARCHAR, 
	allowed_tools VARCHAR, 
	shell_command VARCHAR, 
	caller_id VARCHAR, 
	engine VARCHAR, 
	last_active DATETIME, "group" TEXT, "metadata" TEXT, working_directory TEXT, provider_variant TEXT, deferred_init_failure TEXT, initial_delivery TEXT, deferred_init_external_owner INTEGER NOT NULL DEFAULT 0, deferred_init_runtime_reclaimed INTEGER NOT NULL DEFAULT 0, session_incarnation_id TEXT, model TEXT, model_honored BOOLEAN, description TEXT, delegate_tools TEXT, identity_verified BOOLEAN, 
	PRIMARY KEY (id)
);

CREATE TABLE vault_exclusion (
	vault_id VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	scope_id VARCHAR DEFAULT '' NOT NULL, 
	cao_key VARCHAR NOT NULL, 
	last_known_relpath VARCHAR NOT NULL, 
	content_sha256 VARCHAR, 
	key_source VARCHAR, 
	key_source_reason VARCHAR, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (vault_id, scope, scope_id, cao_key)
);

CREATE TABLE vault_finding (
	id VARCHAR NOT NULL, 
	vault_id VARCHAR NOT NULL, 
	vault_relpath VARCHAR NOT NULL, 
	code VARCHAR NOT NULL, 
	severity VARCHAR NOT NULL, 
	detail VARCHAR NOT NULL, 
	reconcile_run_id VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE vault_migration_receipt (
	receipt_id VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	scope_id VARCHAR DEFAULT '' NOT NULL, 
	cao_key VARCHAR NOT NULL, 
	native_relpath VARCHAR NOT NULL, 
	native_snapshot_sha256 VARCHAR NOT NULL, 
	vault_id VARCHAR NOT NULL, 
	managed_relpath VARCHAR NOT NULL, 
	vault_note_uid VARCHAR NOT NULL, 
	published_content_sha256 VARCHAR NOT NULL, 
	superseded_edges TEXT DEFAULT '[]' NOT NULL, 
	status VARCHAR DEFAULT 'active' NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (receipt_id)
);

CREATE TABLE vault_note (
	note_uid VARCHAR NOT NULL, 
	vault_id VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	scope_id VARCHAR DEFAULT '' NOT NULL, 
	cao_key VARCHAR NOT NULL, 
	vault_relpath VARCHAR NOT NULL, 
	managed BOOLEAN NOT NULL, 
	content_sha256 VARCHAR, 
	frontmatter_sha256 VARCHAR, 
	size_bytes INTEGER, 
	mtime_ns INTEGER, 
	status VARCHAR NOT NULL, 
	last_reconciled_at DATETIME, 
	key_source VARCHAR, 
	key_source_reason VARCHAR, 
	PRIMARY KEY (note_uid), 
	CONSTRAINT uq_vault_note_key UNIQUE (vault_id, scope, scope_id, cao_key), 
	CONSTRAINT uq_vault_note_path UNIQUE (vault_id, vault_relpath)
);

CREATE TABLE vault_note_alias (
	vault_id VARCHAR NOT NULL, 
	former_relpath VARCHAR NOT NULL, 
	cao_key VARCHAR NOT NULL, 
	scope VARCHAR, 
	scope_id VARCHAR, 
	content_sha256 VARCHAR, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (vault_id, former_relpath)
);

CREATE TABLE vault_recall_counter (
	vault_id VARCHAR NOT NULL, 
	counter_name VARCHAR NOT NULL, 
	value INTEGER DEFAULT '0' NOT NULL, 
	PRIMARY KEY (vault_id, counter_name)
);

CREATE TABLE workflow_index (name TEXT PRIMARY KEY, source_path TEXT NOT NULL, mode TEXT NOT NULL, step_count INTEGER, description TEXT NOT NULL DEFAULT '', indexed_at TEXT NOT NULL);

CREATE TABLE workflow_outcomes (
	id VARCHAR NOT NULL, 
	session_name VARCHAR NOT NULL, 
	workflow_name VARCHAR, 
	task_label VARCHAR NOT NULL, 
	agent_profile VARCHAR, 
	source_terminal_id VARCHAR, 
	success BOOLEAN NOT NULL, 
	score INTEGER, 
	friction_notes TEXT NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE workflow_plan_approval (plan_id TEXT PRIMARY KEY, approved_at TEXT NOT NULL, approved_by TEXT NOT NULL);

CREATE TABLE workflow_run (run_id TEXT PRIMARY KEY, workflow_name TEXT NOT NULL, spec_snapshot TEXT NOT NULL, inputs_json TEXT NOT NULL, state TEXT NOT NULL, current_step_id TEXT, started_at TEXT NOT NULL, finished_at TEXT, tier TEXT NOT NULL DEFAULT 'yaml', generation TEXT NOT NULL DEFAULT '1', manifest_json TEXT DEFAULT NULL, error TEXT DEFAULT NULL, kind TEXT DEFAULT NULL, caller_id TEXT DEFAULT NULL);

CREATE TABLE workflow_run_event (run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_type TEXT NOT NULL, event_schema_version INTEGER NOT NULL, ts TEXT NOT NULL, step_id TEXT, attempt INTEGER, state TEXT, elapsed_ms INTEGER, provider TEXT, agent_profile TEXT, engine TEXT, terminal_id TEXT, terminal_offset_start INTEGER, terminal_offset_len INTEGER, error_kind TEXT, reason TEXT, validation_result TEXT, output_ref TEXT, iteration INTEGER, which_guard_fired TEXT, PRIMARY KEY (run_id, seq));

CREATE TABLE workflow_run_seq (run_id TEXT PRIMARY KEY, high_water INTEGER NOT NULL);

CREATE TABLE workflow_run_step (run_id TEXT NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, output_json TEXT, error TEXT, updated_at TEXT NOT NULL, call_fingerprint TEXT DEFAULT NULL, terminal_id TEXT DEFAULT NULL, reprompted INTEGER DEFAULT NULL, error_kind TEXT DEFAULT NULL, result_json TEXT DEFAULT NULL, PRIMARY KEY (run_id, step_id));
