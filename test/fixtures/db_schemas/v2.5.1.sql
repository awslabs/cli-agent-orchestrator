-- Schema from v2.5.1, commit d98243e7149912bf66cb7572e77629ffa0c650af; DDL only, no rows.
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

CREATE TABLE memory_metadata (
	id VARCHAR NOT NULL, 
	"key" VARCHAR NOT NULL, 
	memory_type VARCHAR NOT NULL, 
	scope VARCHAR NOT NULL, 
	scope_id VARCHAR, 
	file_path VARCHAR NOT NULL, 
	tags VARCHAR NOT NULL, 
	source_provider VARCHAR, 
	source_terminal_id VARCHAR, 
	token_estimate INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	access_count INTEGER DEFAULT '0' NOT NULL, 
	last_accessed_at DATETIME, 
	last_compiled_at DATETIME, 
	related_keys TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_memory_key_scope UNIQUE ("key", scope, scope_id), 
	CONSTRAINT ck_related_keys_length CHECK (related_keys IS NULL OR length(related_keys) < 1024)
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

CREATE TABLE terminals (
	id VARCHAR NOT NULL, 
	tmux_session VARCHAR NOT NULL, 
	tmux_window VARCHAR NOT NULL, 
	provider VARCHAR NOT NULL, 
	agent_profile VARCHAR, 
	working_directory VARCHAR, 
	allowed_tools VARCHAR, 
	shell_command VARCHAR, 
	caller_id VARCHAR, 
	engine VARCHAR, 
	"group" TEXT, 
	metadata TEXT, 
	last_active DATETIME, 
	PRIMARY KEY (id)
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

CREATE TABLE workflow_run (run_id TEXT PRIMARY KEY, workflow_name TEXT NOT NULL, spec_snapshot TEXT NOT NULL, inputs_json TEXT NOT NULL, state TEXT NOT NULL, current_step_id TEXT, started_at TEXT NOT NULL, finished_at TEXT, tier TEXT NOT NULL DEFAULT 'yaml', generation TEXT NOT NULL DEFAULT '1', manifest_json TEXT DEFAULT NULL, error TEXT DEFAULT NULL);

CREATE TABLE workflow_run_event (run_id TEXT NOT NULL, seq INTEGER NOT NULL, event_type TEXT NOT NULL, event_schema_version INTEGER NOT NULL, ts TEXT NOT NULL, step_id TEXT, attempt INTEGER, state TEXT, elapsed_ms INTEGER, provider TEXT, agent_profile TEXT, engine TEXT, terminal_id TEXT, terminal_offset_start INTEGER, terminal_offset_len INTEGER, error_kind TEXT, reason TEXT, validation_result TEXT, output_ref TEXT, iteration INTEGER, which_guard_fired TEXT, PRIMARY KEY (run_id, seq));

CREATE TABLE workflow_run_seq (run_id TEXT PRIMARY KEY, high_water INTEGER NOT NULL);

CREATE TABLE workflow_run_step (run_id TEXT NOT NULL, step_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, output_json TEXT, error TEXT, updated_at TEXT NOT NULL, call_fingerprint TEXT DEFAULT NULL, terminal_id TEXT DEFAULT NULL, reprompted INTEGER DEFAULT NULL, error_kind TEXT DEFAULT NULL, result_json TEXT DEFAULT NULL, PRIMARY KEY (run_id, step_id));

CREATE INDEX idx_memory_rel_lookup ON memory_relationships (scope, scope_id, source_key);

CREATE INDEX idx_memory_scope ON memory_metadata (scope, scope_id);

CREATE INDEX idx_memory_type ON memory_metadata (memory_type);

CREATE INDEX idx_memory_updated ON memory_metadata (updated_at);

CREATE INDEX idx_outcome_agent ON workflow_outcomes (agent_profile, created_at);

CREATE INDEX idx_outcome_session ON workflow_outcomes (session_name, created_at);

CREATE INDEX idx_workflow_run_started_at ON workflow_run (started_at);

CREATE INDEX idx_workflow_run_state ON workflow_run (state);

CREATE INDEX ix_project_aliases_project_id ON project_aliases (project_id);

CREATE UNIQUE INDEX uq_memory_key_scope_null ON memory_metadata ("key", scope) WHERE scope_id IS NULL;

CREATE UNIQUE INDEX uq_memory_rel ON memory_relationships (scope, scope_id, source_key, target_key, type, origin);
