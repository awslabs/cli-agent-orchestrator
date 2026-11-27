"""Recorded 3.0-beta1 baseline layout, including migrator-only tables.

Extra columns and tables are accepted; missing non-unique indexes warn only.
The NULL-scope memory index is tolerated when duplicate legacy rows prevent it.
"""

TOLERATED_UNIQUE_INDEXES = frozenset({"uq_memory_key_scope_null"})

_COLUMNS = """
decision_records id created_at updated_at contract_version point state decider decider_version kind provider agent_profile target_kind fallback_value fallback_source answer probabilities confidence candidate_value candidate_model applied_value outcome reason launched_model model_honored latency_ms decision_status launch_status terminal_id message_hash hash_key_id message_bytes options fallback_enforcement applied_enforcement match_rule match_needs match_covered match_tiebreak match_from match_pick catalog_version catalog_hash
delegation_edges id caller_id worker_id source state reason message_id created_at delivered_at closed_at
ephemeral_agents name owner_kind owner_id session_name state claim_id idempotency_key claim_expires_at launched_terminal_id bound_at model_tier effort provider effective_tools created_at expires_at gc_reason spec_sha256 profile_sha256 audit_path
flows name file_path schedule agent_profile provider script last_run next_run enabled
handoff_results job_id state terminal_id last_message error_message created_at updated_at
idempotency_keys key terminal_id request_fingerprint created_at
inbox id sender_id receiver_id message status created_at
kept_terminals terminal_id session_name kept_at kept_until reason
launch_records id terminal_id session_name provider target_name target_kind launched_model model_honored provider_session_id record_ids caller_id outcome outcome_recorded_at outcome_reporter_id created_at ended_at end_reason
launch_status_events id terminal_id status created_at
memory_metadata id key memory_type scope scope_id source_kind file_path tags source_provider source_terminal_id token_estimate created_at updated_at access_count last_accessed_at last_compiled_at related_keys
memory_relationships id scope scope_id source_key target_key type origin status confidence rank attributes_json source_updated_at created_at updated_at
project_aliases alias project_id kind created_at
session_incarnations session_name incarnation_id
terminals id description tmux_session tmux_window provider agent_profile model model_honored working_directory runner_id allowed_tools delegate_tools shell_command caller_id engine provider_variant group metadata deferred_init_failure initial_delivery deferred_init_external_owner deferred_init_runtime_reclaimed session_incarnation_id identity_verified last_active
vault_exclusion vault_id scope scope_id cao_key last_known_relpath content_sha256 key_source key_source_reason created_at
vault_finding id vault_id vault_relpath code severity detail reconcile_run_id created_at
vault_migration_receipt receipt_id scope scope_id cao_key native_relpath native_snapshot_sha256 vault_id managed_relpath vault_note_uid published_content_sha256 superseded_edges status created_at
vault_note note_uid vault_id scope scope_id cao_key vault_relpath managed content_sha256 frontmatter_sha256 size_bytes mtime_ns status last_reconciled_at key_source key_source_reason
vault_note_alias vault_id former_relpath cao_key scope scope_id content_sha256 created_at
vault_recall_counter vault_id counter_name value
workflow_index name source_path mode step_count description indexed_at
workflow_outcomes id session_name workflow_name task_label agent_profile source_terminal_id success score friction_notes created_at
workflow_plan_approval plan_id approved_at approved_by
workflow_run run_id workflow_name spec_snapshot inputs_json state current_step_id started_at finished_at tier generation manifest_json error kind caller_id
workflow_run_event run_id seq event_type event_schema_version ts step_id attempt state elapsed_ms provider agent_profile engine terminal_id terminal_offset_start terminal_offset_len error_kind reason validation_result output_ref iteration which_guard_fired
workflow_run_seq run_id high_water
workflow_run_step run_id step_id state attempts output_json error updated_at call_fingerprint terminal_id reprompted error_kind result_json
"""

_UNIQUE_INDEXES = {
    "memory_metadata": frozenset({"uq_memory_key_scope_null"}),
    "memory_relationships": frozenset({"uq_memory_rel"}),
}

_NONUNIQUE_INDEXES = """
decision_records ix_decision_records_terminal_id ix_decision_records_point_created_at ix_decision_records_created_at
delegation_edges ix_delegation_edges_worker_id ix_delegation_edges_state ix_delegation_edges_caller_id
ephemeral_agents ix_ephemeral_agents_launched_terminal_id
kept_terminals ix_kept_terminals_session_name
launch_records ix_launch_records_terminal_id ix_launch_records_ended_at ix_launch_records_created_at
launch_status_events ix_launch_status_events_terminal_created
memory_metadata idx_memory_updated idx_memory_type idx_memory_scope
memory_relationships idx_memory_rel_lookup
project_aliases ix_project_aliases_project_id
workflow_outcomes idx_outcome_session idx_outcome_agent
workflow_run idx_workflow_run_state idx_workflow_run_started_at
"""
NONUNIQUE_INDEXES_V1 = {
    fields[0]: frozenset(fields[1:])
    for fields in (line.split() for line in _NONUNIQUE_INDEXES.strip().splitlines())
}

SCHEMA_MANIFEST_V1 = {
    fields[0]: (frozenset(fields[1:]), _UNIQUE_INDEXES.get(fields[0], frozenset()))
    for fields in (line.split() for line in _COLUMNS.strip().splitlines())
}
