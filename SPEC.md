# SPEC: Service Release & Operations Console (osprey-core)

A Flask console, deployed to Cloud Run behind IAP, for releasing and operating osprey-core.
Status: implemented in this repository. Each acceptance criterion below lists the pytest tests that
prove it (`tests/<file>.py::<test>`; `tests/test_honesty.py` checks that every listed test exists).
Browser behavior is also swept by `qa/e2e_console.py` (desktop 1440x900 and mobile 390x844, light
and dark) and HTTP behavior by `qa/smoke_endpoints.py`, both run against a live server.

Honesty rule: computed values (SLOs, the DP-001 scorecard, receipt signatures, the serving revision)
are computed live. Illustrative content (the sample release scenario, the rollout ledger, projected
task keys, the curated DP-001 scenario traces) carries a `source_label` in its config, returned by
the API and shown in the UI.

## Acceptance criteria

### AC-01 Workflow stage timeline
GET /api/workflow returns S1_SPEC, S2_TESTS, S3_BUILD, S4_VERIFY_QA (agentic) and S5_RELEASE_GATE,
S6_DEPLOY_CLOSE (deterministic) in order, with owners, entry and exit gates, evidence, and a status
derived from `current_stage`. The Workflow tab shows the timeline with a Hybrid badge; agentic and
deterministic stages look different.
- tests/test_api_core.py::test_workflow_statuses_are_derived_from_current_stage
- tests/test_api_core.py::test_workflow_follows_config_changes
- tests/test_api_core.py::test_workflow_unknown_current_stage_is_503
- tests/test_repo_hygiene.py::test_workflow_yaml_matches_config
- tests/test_workflow_wiring.py::test_yaml_and_console_describe_the_same_stages

### AC-02 Stage drawers
Selecting a stage opens a modal drawer (dialog with aria-modal) listing owners from the 25-agent
roster, gates, evidence, and status. Esc, an overlay click, and the close button dismiss it; focus
stays inside while open and returns to the stage afterwards.
- tests/test_repo_hygiene.py::test_index_html_accessibility_contract
- tests/test_api_core.py::test_agents_roster_has_all_25_unique_agents
- tests/test_repo_hygiene.py::test_docs_cover_roster_and_stages

### AC-03 Pluggable task backends
Six backends (farmtable, github, jira, gitlab, beads, linear) come from config/task_backends.json.
POST /api/task-backend selects one for the process, echoes it, and returns how S1 to S6 map to
backend-native task keys and statuses (labelled as a projection). Unknown or malformed input
returns a structured 400.
- tests/test_api_ops.py::test_task_backends_registry_lists_six
- tests/test_api_ops.py::test_select_each_backend_is_echoed_and_persisted
- tests/test_api_ops.py::test_unknown_backend_is_400_and_selection_unchanged
- tests/test_api_ops.py::test_backend_value_edge_cases_are_400
- tests/test_units.py::test_unsafe_or_broken_formats_are_rejected
- tests/test_units.py::test_selector_defaults_persists_and_is_thread_safe

### AC-04 DP-001 PreToolUse guard
dreamstore/hooks/dp001_pre_tool_guard.py reads one tool-call event on stdin, prints a JSON
decision, and exits 0 (allow) or 2 (block, reason on stderr). It fails closed on empty, malformed,
oversized, or slow input and on an unreadable policy. The versioned policy blocks force-push to
main, hard resets to the remote, recursive deletes of protected locations (normalized flag and
target forms), service deletion, public or non-IAP deploys, deploys before S5, CI/CD commands before
their stage (AC-16), namespace deletion, secret reads and echoes, impersonation, and writes to
protected paths.
- tests/test_guard.py::test_v11_blocks
- tests/test_guard.py::test_v11_allows
- tests/test_guard.py::test_v11_file_write_gates
- tests/test_guard.py::test_malformed_events_fail_closed
- tests/test_guard.py::test_policy_validation_rejects_defects
- tests/test_guard_normalize.py::test_delete_evasions_are_blocked
- tests/test_guard_normalize.py::test_routine_cleanup_stays_allowed
- tests/test_guard_normalize.py::test_kubectl_secret_reads_are_blocked_in_any_form
- tests/test_guard_normalize.py::test_working_tree_and_find_deletes_are_blocked
- tests/test_guard_normalize.py::test_scoped_find_and_cleanup_stay_allowed
- tests/test_guard_cli.py::test_block_exits_two_with_reason_on_stderr
- tests/test_guard_cli.py::test_bad_stdin_fails_closed
- tests/test_guard_cli.py::test_missing_policy_fails_closed
- tests/test_guard_cli.py::test_slow_stdin_times_out_and_blocks

### AC-05 Scorecard computed by replay (60% to 100%)
The ops-release-guardrails scorecard is computed by replaying the curated scenario traces through
`evaluate()` under v1.0.0 and v1.1.0: baseline 6/10 (60%), candidate 10/10 (100%), gate PASS.
GET /api/dreams returns per-case results; POST /api/dreams/replay recomputes them with measured
latency; changing the policy changes the numbers.
- tests/test_api_ops.py::test_dreams_scorecard_is_computed_live
- tests/test_api_ops.py::test_dreams_replay_post_recomputes_with_new_trace
- tests/test_api_ops.py::test_dreams_numbers_follow_the_policy_not_a_constant
- tests/test_dreaming.py::test_replay_scores_baseline_60_and_candidate_100
- tests/test_dreaming.py::test_swapped_versions_fail_the_gate
- tests/test_guard_normalize.py::test_policy_versions_are_byte_identical_to_the_pushed_baseline

### AC-06 Evolver and DP-001 drawer
scripts/nightly_dream_evolver.py writes dreamstore/scorecard.json and
dreamstore/proposals/DP-001.json (plus a dated synthesis with --write), prints a JSON summary, and
exits 1 on a regression or a configuration error. The DP-001 drawer shows the proposal, the
scorecard, per-case rows, and a re-run action.
- tests/test_dreaming.py::test_evolver_writes_computed_artifacts
- tests/test_dreaming.py::test_evolver_exits_nonzero_on_regression
- tests/test_dreaming.py::test_evolver_config_error_writes_nothing
- tests/test_dreaming.py::test_evolver_runs_as_a_standalone_script
- tests/test_dreaming.py::test_committed_scorecard_matches_live_replay

### AC-07 HMAC audit receipts (verify and tamper)
Receipts are canonical JSON signed with HMAC-SHA256 using AUDIT_HMAC_KEY and chained through
`prev_signature`. Without a key the API returns 503. Verification uses `hmac.compare_digest`;
tampering with any field, or removing or reordering receipts, yields FAIL.
- tests/test_api_ops.py::test_receipts_chain_is_signed_and_linked
- tests/test_api_ops.py::test_receipts_fail_closed_without_key
- tests/test_api_ops.py::test_verify_single_receipt_passes
- tests/test_api_ops.py::test_tampered_receipt_fails
- tests/test_api_ops.py::test_broken_chain_is_detected
- tests/test_api_ops.py::test_odd_signatures_fail_without_crashing
- tests/test_units.py::test_chain_is_deterministic_and_linked

### AC-08 Institutional Slate theme and #theme-toggle
Light background #F8FAFC and dark #0C0E14 through CSS custom properties. #theme-toggle exposes
aria-pressed, persists the choice in localStorage, and honors prefers-color-scheme on first load.
Icons are inline monoline SVG (no emoji); contrast meets WCAG AA; layouts work at 1440x900 and
390x844 without horizontal scrolling, and table captions fill the full width on mobile.
- tests/test_repo_hygiene.py::test_css_defines_both_themes_and_hides_nothing_essential
- tests/test_repo_hygiene.py::test_mobile_table_captions_fill_full_width
- tests/test_repo_hygiene.py::test_index_html_accessibility_contract
- tests/test_repo_hygiene.py::test_ui_files_are_ascii_without_external_resources

### AC-09 IAP-only deployment
The image runs gunicorn as a non-root user on 0.0.0.0:${PORT:-8080}. Deploys use --iap and
--no-allow-unauthenticated; the guard blocks deploys without --iap, public deploys, and public IAM
bindings.
- tests/test_repo_hygiene.py::test_dockerfile_contract
- tests/test_repo_hygiene.py::test_every_from_line_is_digest_pinned
- tests/test_guard.py::test_baseline_differs_exactly_where_dp001_says
- tests/test_guard_cli.py::test_version_override_selects_baseline

### AC-10 S6 rollback
scripts/s6_rollback.py runs as a dry-run by default. With --execute it rolls the Cloud Deploy target
back (`gcloud deploy targets rollback`) with a fallback that shifts traffic to the previous ready Cloud
Run revision, and it exits 1 without touching traffic on 401, 403, 429, 503, timeouts, or an ambiguous
history.
- tests/test_rollback.py::test_dry_run_plans_without_touching_traffic
- tests/test_rollback.py::test_execute_shifts_traffic_with_planned_command
- tests/test_rollback.py::test_gcloud_http_failures_fail_closed
- tests/test_rollback.py::test_timeout_fails_closed
- tests/test_rollback.py::test_ambiguous_history_fails_closed
- tests/test_rollback.py::test_auto_mode_plans_a_cloud_deploy_rollback_to_the_previous_release
- tests/test_rollback.py::test_execute_runs_the_rollback_and_polls_its_rollout_to_success
- tests/test_rollback.py::test_auto_falls_back_to_traffic_without_a_previous_release
- tests/test_rollback.py::test_cloud_deploy_errors_fail_closed_without_traffic_changes

### AC-11 Fail-closed configuration
Every tunable lives in config/*.json, validated by config/config_schema.json. Missing, unreadable,
malformed, or invalid config returns a structured 503 and never a fake default, including files
that disappear while the server runs. No magic numbers or inline data tables in production code.
- tests/test_config_and_chaos.py::test_file_removed_mid_run_raises_instead_of_serving_stale
- tests/test_config_and_chaos.py::test_os_error_on_read_is_unreadable
- tests/test_config_and_chaos.py::test_schema_violations_are_typed
- tests/test_config_and_chaos.py::test_api_recovers_after_config_returns
- tests/test_config_and_chaos.py::test_concurrent_requests_while_config_flaps_never_500
- tests/test_api_core.py::test_readyz_503_when_any_config_is_broken
- tests/test_api_core.py::test_http_error_codes_come_from_config_not_the_interpreter
- tests/test_api_ops.py::test_non_json_content_type_is_structured_415
- tests/test_api_ops.py::test_ui_fetch_sends_json_content_type
- tests/test_ast_zero_hardcoding.py::test_wire_codes_never_use_interpreter_enum_names
- tests/test_ast_zero_hardcoding.py::test_no_magic_numbers
- tests/test_ast_zero_hardcoding.py::test_no_inline_data_literals

### AC-12 Telemetry
Every request and core operation logs one JSON line with a uuid4 trace_id, a perf_counter latency
in integer milliseconds, a status, and an error taxonomy. GET /api/telemetry exposes a bounded
ring, and the SLO cards are computed from it.
- tests/test_api_core.py::test_telemetry_returns_measured_events_with_trace_ids
- tests/test_api_core.py::test_request_telemetry_is_json_on_stdout
- tests/test_api_core.py::test_health_reports_insufficient_data_then_healthy
- tests/test_api_core.py::test_health_breaches_when_server_errors_dominate
- tests/test_ast_zero_hardcoding.py::test_no_randomness_in_production_python

### AC-13 Honest labelling
The sample release scenario (workflow progress and receipts), the illustrative rollout ledger, the
projected task keys, and the curated DP-001 scenario traces carry a `source_label` that the API
returns and the UI shows. The sample flag is part of each signed receipt. DP-001 provenance
describes curated traces, never captured sessions, and every repo path named in configs and docs
exists. Runtime evidence under `.workflow/evidence/` is git-ignored and never committed.
- tests/test_honesty.py::test_illustrative_configs_declare_their_provenance
- tests/test_honesty.py::test_apis_return_source_labels
- tests/test_honesty.py::test_scenario_flag_is_signed
- tests/test_honesty.py::test_ui_renders_every_label
- tests/test_honesty.py::test_no_recorded_provenance_claims
- tests/test_honesty.py::test_path_references_resolve
- tests/test_honesty.py::test_runtime_evidence_paths_are_git_ignored

### AC-14 Health paths that work on Cloud Run
GET /health (liveness) and GET /ready (readiness) avoid the paths ending in z that Cloud Run's
front end reserves; /healthz and /readyz remain for container-local checks.
- tests/test_api_core.py::test_liveness_endpoints_need_no_config
- tests/test_api_core.py::test_health_alias_matches_healthz
- tests/test_api_core.py::test_ready_alias_matches_readyz
- tests/test_api_core.py::test_readyz_reports_missing_key_as_boolean_only

### AC-15 Repository hygiene
No virtual environment or bytecode is tracked, every source file has at most 400 lines, the scrub
for banned customer terms returns nothing, and requirements are pinned.
- tests/test_repo_hygiene.py::test_no_virtualenv_or_bytecode_in_repo
- tests/test_repo_hygiene.py::test_every_source_file_is_at_most_400_lines
- tests/test_repo_hygiene.py::test_scrub_banned_terms_case_insensitive_substring
- tests/test_repo_hygiene.py::test_requirements_are_pinned_and_split

### AC-16 CI/CD stage gates in the DP-001 guard
The active policy blocks Cloud Build CI (`gcloud builds submit`, `scripts/s5_release_gate.py --commit`,
`scripts/workflow_ctl.py release`) before S5_RELEASE_GATE. It blocks Google Cloud Deploy
(`gcloud deploy releases create`, `gcloud deploy apply`, `gcloud deploy targets rollback`,
`scripts/s6_deploy.py --packet`, `scripts/s6_rollback.py --execute`) before S6_DEPLOY_CLOSE. An unknown
or missing stage blocks them all. Read-only CI/CD commands stay allowed. The baseline v1.0.0 has no CI/CD
gates, and the DP-001 replay still scores 6/10 to 10/10.
- tests/test_guard.py::test_cicd_commands_are_stage_gated
- tests/test_guard.py::test_cicd_gates_fail_closed_without_a_known_stage
- tests/test_guard.py::test_cicd_reads_and_dry_runs_stay_allowed_before_s5
- tests/test_guard.py::test_compound_cicd_command_needs_the_latest_stage
- tests/test_guard.py::test_baseline_has_no_cicd_gates
- tests/test_guard_cli.py::test_cicd_gates_read_the_stage_from_the_environment
- tests/test_dreaming.py::test_rule_diff_is_computed_from_policy

### AC-17 Hybrid workflow wiring and control loops
`.workflow/workflow.yaml` declares S1 to S4 as agentic stages graded by deterministic exit gates and S5,
S6 as deterministic stages. S5 runs `python3 scripts/s5_release_gate.py --commit <HEAD sha>` (Cloud Build
CI and the immutable @sha256 release packet) and bounces a failed step to S3_BUILD (unit-tests,
docker-build, docker-push) or S4_VERIFY_QA (guard-substitutions, container-smoke, release-packet). S6
runs `scripts/s6_deploy.py` (Google Cloud Deploy), probes IAP with `scripts/s6_exit.py`, rolls back with
`scripts/s6_rollback.py --execute`, re-probes, and bounces to S3_BUILD. Stage ids match
`config/cicd.json`, `config/workflow_stages.json`, and the DP-001 stage order. Every owner is on the
roster, no QA agent is a builder, and failure modes 1 to 5 follow the workflow kit.
- tests/test_workflow_wiring.py::test_contract_stage_ids_follow_the_workflow_order
- tests/test_workflow_wiring.py::test_guard_stage_order_follows_the_workflow
- tests/test_workflow_wiring.py::test_agentic_stages_are_graded_and_deterministic_stages_run_scripts
- tests/test_workflow_wiring.py::test_agentic_exit_gates_point_at_real_checks
- tests/test_workflow_wiring.py::test_s5_bounces_each_cloud_build_step_to_the_stage_that_owns_the_fix
- tests/test_workflow_wiring.py::test_s6_probes_rolls_back_then_bounces_to_build
- tests/test_workflow_wiring.py::test_every_owner_is_on_the_roster_and_qa_is_independent
- tests/test_workflow_wiring.py::test_failure_modes_follow_the_kit
- tests/test_workflow_wiring.py::test_workflow_commands_pass_the_guard_only_from_their_stage

### AC-18 CI/CD definitions and status at S5 and S6
GET /api/workflow gives S5 and S6 a `pipeline` block (Cloud Build with `cloudbuild.yaml`; Google Cloud
Deploy with `clouddeploy.yaml`) and `bounce_to` routes. `clouddeploy.yaml` defines the delivery pipeline
osprey-core-cd with the target osprey-core-prod on Cloud Run us-central1 and the execution service
account osprey-onboard-sa. The Cloud Build step ids match the S5 bounce routes. The console shows live
Cloud Build and Cloud Deploy status from GET /api/pipeline on the S5 and S6 stages. `WORKFLOW.md` and
`README.md` state every deterministic command.
- tests/test_api_core.py::test_workflow_exposes_bounce_routes_and_pipelines
- tests/test_workflow_wiring.py::test_ci_and_cd_blocks_match_the_contract
- tests/test_workflow_wiring.py::test_cloud_deploy_file_matches_the_contract
- tests/test_workflow_wiring.py::test_cloud_build_steps_match_the_bounce_routes
- tests/test_workflow_wiring.py::test_contract_route_table_agrees_when_declared
- tests/test_workflow_wiring.py::test_workflow_doc_states_every_deterministic_command
- tests/test_workflow_wiring.py::test_readme_documents_ci_cd
- tests/test_cicd_manifests.py::test_image_is_tagged_with_short_sha_only
- tests/test_cicd_manifests.py::test_release_packet_carries_every_field_and_the_pushed_digest
- tests/test_cicd_manifests.py::test_run_service_preserves_live_service_settings
- tests/test_cicd_manifests.py::test_clouddeploy_pipeline_target_location_and_execution_sa
- tests/test_api_pipeline.py::test_default_document_is_honestly_not_configured
- tests/test_api_pipeline.py::test_chaos_answers_200_with_a_degraded_document
- tests/test_pipeline_status.py::test_happy_path_maps_build_and_newest_rollout
- tests/test_ui_pipeline_contract.py::test_stage_ids_come_from_the_api_never_from_javascript

### AC-19 Deterministic S5 and S6 scripts and the control loop
`scripts/s5_release_gate.py` submits Cloud Build for a clean checkout of the commit, cross-checks the
pushed digest against the build record and the uploaded packet, and seals the packet; a failed step
returns its bounce route and transient faults exit 75. `scripts/s6_deploy.py` creates the Cloud Deploy
release from the packet digest only and verifies that the serving revision runs it. `scripts/s6_exit.py`
fails on any IAP, provenance, service-setting, or IAM drift and never logs the token.
`scripts/workflow_ctl.py release` closes S6 or rolls back, re-probes, and bounces, with a ledger entry per
transition.
- tests/test_s5_release_gate.py::test_happy_path_submits_polls_crosschecks_and_writes_a_sealed_packet
- tests/test_s5_release_gate.py::test_first_failed_step_picks_the_bounce_route
- tests/test_s5_release_gate.py::test_transient_faults_exit_75_with_resumable_build_id
- tests/test_s6_deploy.py::test_happy_path_creates_release_polls_rollout_and_verifies_serving
- tests/test_s6_deploy.py::test_succeeded_rollout_must_serve_the_packet_with_provenance
- tests/test_s6_exit.py::test_verify_passes_every_check_and_never_leaks_the_token
- tests/test_s6_exit.py::test_http_gate_failures
- tests/test_s6_exit.py::test_service_and_iam_drift_fails
- tests/test_workflow_ctl.py::test_pass_path_closes_and_ledgers_every_transition
- tests/test_workflow_ctl.py::test_s5_failure_bounces_without_touching_s6
- tests/test_workflow_ctl.py::test_exit_gate_failure_rolls_back_reprobes_and_bounces

### AC-20 Agent template export
`templates/` holds the 13 agent templates (10 project templates and the qa-gemini, qa-opus, qa-fable
council) exported verbatim from the Hub. `templates/MANIFEST.json` records every file's sha256 and size.
- tests/test_templates_export.py::test_exactly_13_template_dirs_plus_manifest_and_readme
- tests/test_templates_export.py::test_manifest_matches_dirs_ids_hashes_and_count
- tests/test_templates_export.py::test_template_metadata_files_and_hashes
- tests/test_templates_export.py::test_qa_council_harness_model_and_blob_source
- tests/test_templates_export.py::test_no_secret_patterns_or_project_numbers

## Out of scope

- Live workflow tracking: stage progress and receipts are a labelled sample scenario.
- Creating tickets in the selected task backend: the mapping is a projection.
- Live Cloud Run traffic in the rollout ledger: the ledger is sample data; the header shows the live revision.
- Starting CI/CD from the console: the console only reads pipeline status; releases run from the S5 and S6 scripts.
