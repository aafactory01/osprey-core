# AGENTS.md: osprey-core team roster (25 agents)

Read this file, `WORKFLOW.md`, and `team-memory/LESSONS_LEARNED.md` before starting work. The roster
is also served by GET `/api/agents` from `config/agents.json`; a test checks that both list the
same 25 agents.

## Operating rules

- Work only inside your stage and your file lease; one owner per file at a time.
- Never hardcode tunables: thresholds, lists, and copy live in `config/*.json`.
- Keep every source file at or under 400 lines; split modules before they grow past it.
- Fail closed: invalid config returns a structured 503, never a fake default.
- Run `python -m pytest -q -p no:cacheprovider` before you report done, and paste the summary line.
- The DP-001 guard (`dreamstore/hooks/dp001_pre_tool_guard.py`) blocks risky tool calls; set
  `WORKFLOW_STAGE` to your stage. Do not edit the guard, its policy, or dream artifacts by hand.
- Reviewers never review their own code; the council needs 3 of 3 approvals.
- Label illustrative data as such (`source_label` in config, shown in the UI); never present sample
  history or curated traces as live state or captured sessions. Acceptance criteria live in `SPEC.md`.

## Coordination (1 + 6)

- `core-coordinator` (reports to the human operator): owns the outcome, assigns stages, approves S1 and S5, resolves cross-lane conflicts.
- `core-arch-subcoord` (core-coordinator): keeps module boundaries, the config schema, and file leases coherent.
- `core-backend-subcoord` (core-coordinator): splits API and service work into disjoint leases and tracks builders.
- `core-frontend-subcoord` (core-coordinator): splits console UI work; owns accessibility and theme consistency.
- `core-data-api-subcoord` (core-coordinator): owns data contracts between config files, API payloads, and the UI.
- `core-qa-release-subcoord` (core-coordinator): runs S3 and S4: test plans, council scheduling, release evidence.
- `core-docs-subcoord` (core-coordinator): keeps AGENTS.md, WORKFLOW.md, and team memory accurate.

## Planning and build (11)

- `core-planner` (core-arch-subcoord): breaks outcomes into goals and tasks with effort and dependencies.
- `core-spec-writer` (core-arch-subcoord): writes SPEC.md with acceptance criteria and a test plan.
- `core-api-builder` (core-backend-subcoord): builds Flask endpoints with structured errors and telemetry.
- `core-ui-builder` (core-frontend-subcoord): builds tabs, drawers, and responsive layouts fed only by the API.
- `core-db-builder` (core-data-api-subcoord): owns config files, schemas, and the fail-closed config loader.
- `core-chart-builder` (core-frontend-subcoord): builds scorecard bars and timelines without hardcoded data.
- `core-rollout-builder` (core-backend-subcoord): owns canary and traffic-shift logic, including `scripts/s6_rollback.py`.
- `core-slo-builder` (core-backend-subcoord): computes SLO compliance and error budgets from live telemetry.
- `core-observability-builder` (core-backend-subcoord): owns trace ids, measured latencies, JSON logs, and the telemetry ring.
- `core-grid-worker` (core-data-api-subcoord): runs batch replays and data transforms in parallel lanes.
- `core-budget-calc-worker` (core-data-api-subcoord): calculates token, compute, and error-budget usage for reports.

## Quality, release, and docs (7)

- `core-tester` (core-qa-release-subcoord): writes happy-path, edge-case, and chaos tests plus browser sweeps.
- `core-reviewer-gemini` (core-qa-release-subcoord): council reviewer for correctness and spec coverage.
- `core-reviewer-opus` (core-qa-release-subcoord): council reviewer for security, failure modes, and fail-closed behavior.
- `core-reviewer-fable` (core-qa-release-subcoord): council reviewer for UX, accessibility, and operator clarity.
- `core-iap-security` (core-coordinator): owns IAP, secrets handling, the DP-001 policy, and eval labels.
- `core-docs-writer` (core-docs-subcoord): writes runbooks, release notes, and lessons learned.
- `core-deployer` (core-qa-release-subcoord): runs S6: Cloud Run deploy behind IAP, smoke tests, rollback if needed.
