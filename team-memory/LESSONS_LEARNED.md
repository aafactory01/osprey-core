# Lessons learned

Generic, reusable lessons for agents working on the release console. Add one line per resolved
incident; keep each lesson testable.

- Compute scorecards, never assert them: a number shown in the UI must come from code that ran (the shadow replay), not from a JSON file someone typed.
- Fail closed on configuration: a missing or malformed config file returns a structured 503 with the file and reason; stale caches and silent defaults hide outages.
- Re-check the file on every read (stat signature) so a config file that disappears mid-run is detected immediately instead of serving cached data.
- Keep tunables out of code: thresholds, lists, copy, timeouts, and ports live in `config/*.json`, enforced by an AST test.
- Never invent signatures: without `AUDIT_HMAC_KEY` the receipts API says so (503) rather than returning placeholder hashes.
- Chain audit receipts with `prev_signature` so deleting or reordering one receipt breaks verification of everything after it.
- Compare HMACs with `hmac.compare_digest` over bytes, and treat non-string signatures as a FAIL, not a crash.
- A PreToolUse hook must exit 2 to block; any other non-zero exit lets the tool run, so the hook catches every error and blocks.
- Bound hook input: cap stdin size and read time, and block on empty or malformed events.
- Guardrails need precision as well as recall: an over-broad rule (block every rm -rf) trains agents to route around the guard.
- Gate release commands on the workflow stage; a deploy before the release-gate receipt is a policy violation even when it uses IAP.
- Pass `--quiet` and close stdin for gcloud subprocesses; an interactive prompt otherwise hangs until the timeout.
- Never track virtual environments or bytecode; `.gitignore`, `.dockerignore`, and `.gcloudignore` must exclude them.
- Measure latency with `time.perf_counter()` and report integers; never display synthetic timings.
- Render data with `textContent` only and show explicit loading and error states; never print raw objects or placeholder values.
- One gunicorn worker with threads keeps per-process state (like the selected task backend) consistent on a single instance.
- Label illustrative data where it is shown: sample scenarios, sample ledgers, and projections carry a `source_label` from config that the API returns and the UI renders; never present sample history as live state.
- Describe provenance exactly: curated scenario traces are not recordings, and a manual evolver run is not a nightly run.
- Every file path named in a config or doc must exist; a test resolves them so evidence cannot point at nothing.
- Cloud Run's front end reserves some URL paths ending in z (such as `/healthz`); expose `/health` and `/ready` for checks through the service URL.
- Normalize commands before matching guard rules: split flags (`-r -f`) and relative targets (`../..`, `./*`) are the same risk as their canonical forms.
- Gate CI/CD on the workflow stage like any release command: Cloud Build only from S5, Cloud Deploy only at S6, and a wrapper script is gated by the action flag that makes it act (`--commit`, `--packet`, `--execute`).
- Bounce a failed deterministic check to the stage that owns the fix (a failed unit-tests step goes back to S3_BUILD, a failed smoke check to S4_VERIFY_QA); never let the deterministic stage patch code itself.
- Deploy the immutable `@sha256` digest from the release packet, never a mutable tag, so the probe, the rollback, and the provenance all name the same image.
- Mint S6 IAP exit-probe tokens with `--audiences` set to the IAP OAuth client ID and ensure the probe service account holds `roles/iap.httpsResourceAccessor` on the Cloud Run IAP web resource.

- Keep the Flask application factory lazy and root-scoped: create_app(root) must tolerate a missing or malformed config at startup so liveness (/healthz, /health) still boots and API routes fail closed with 503, and must never fall back to REPO_ROOT when given an explicit root (PR #25 / Issue #22).
