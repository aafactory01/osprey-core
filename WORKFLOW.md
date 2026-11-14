# Hybrid workflow (S1_SPEC to S6_DEPLOY_CLOSE)

Every change to osprey-core moves through six stages. S1 to S4 are **agentic**: agents plan and
execute, and gates check evidence before a stage closes. S5 and S6 are **deterministic**: fixed
scripts run, and no agent may improvise or skip a step. The machine-readable definition is
`.workflow/workflow.yaml`; the console serves the same stages from `config/workflow_stages.json`
(GET `/api/workflow`), and a test keeps the two in sync.

## Stages

- **S1_SPEC** (agentic): core-coordinator, core-planner, core-spec-writer.
  - Exit: SPEC.md with acceptance criteria and a test plan; signed approval receipt.
- **S2_CODE** (agentic): core-backend-subcoord, core-frontend-subcoord, core-api-builder, core-ui-builder, core-db-builder.
  - Exit: every file at most 400 lines; AST zero-hardcoding gate green; commit SHAs per lease.
- **S3_TEST** (agentic): core-qa-release-subcoord, core-tester.
  - Exit: pytest 100% (happy, edge, chaos); Playwright sweep with 0 console errors and 0 anomaly strings.
- **S4_REVIEW** (agentic): core-reviewer-gemini, core-reviewer-opus, core-reviewer-fable.
  - Exit: 3 of 3 council approvals; a reviewer never reviews their own code.
- **S5_RELEASE_GATE** (deterministic): core-coordinator, core-iap-security.
  - Exit: DP-001 replay gate PASS; receipt chain verifies PASS; human approval recorded.
- **S6_DEPLOY_CLOSE** (deterministic): core-deployer.
  - Exit: new revision serves 100% behind IAP; unauthenticated GET is 302 or 401; rollback dry-run green.

## Gates and evidence

- **Receipts.** Each gate decision becomes an HMAC-SHA256 receipt chained to the previous one
  (`config/audit_receipts.json`, GET `/api/receipts`, POST `/api/receipts/verify`). The key comes
  from `AUDIT_HMAC_KEY`; without it the receipts API answers 503 instead of inventing signatures.
- **Task backend.** Stages map to native tasks in the selected backend (farmtable, github, jira,
  gitlab, beads, linear) through `.workflow/task_backend.py` and `config/task_backends.json`.
- **Night Dreaming.** `scripts/nightly_dream_evolver.py` replays recorded tool calls through the
  DP-001 guard under the baseline and candidate policy versions and writes the computed scorecard.

## DP-001 PreToolUse guard

`dreamstore/hooks/dp001_pre_tool_guard.py` reads one tool-call event on stdin and exits `0` (allow)
or `2` (block, reason on stderr). It fails closed on empty, slow, oversized, or malformed input and
on an unreadable policy. The stage comes from the event's `stage` field or `WORKFLOW_STAGE`.

Claude Code (`.claude/settings.json`):

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit",
  "hooks": [{"type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/dreamstore/hooks/dp001_pre_tool_guard.py\""}]}]}}
```

Gemini CLI (`.gemini/settings.json`):

```json
{"hooks": {"BeforeTool": [{"matcher": "run_shell_command|write_file|replace",
  "hooks": [{"type": "command", "command": "python3 dreamstore/hooks/dp001_pre_tool_guard.py"}]}]}}
```

## Deterministic commands

- S5: `python3 scripts/nightly_dream_evolver.py --write` (exit 1 if the candidate scores below the baseline).
- S6 deploy: `WORKFLOW_STAGE=S6_DEPLOY_CLOSE gcloud run deploy osprey-core --source . --region us-central1 --iap --no-allow-unauthenticated --set-secrets AUDIT_HMAC_KEY=<secret>:latest`
- S6 rollback: `python3 scripts/s6_rollback.py --project <project>` (dry-run), then `--execute`.
