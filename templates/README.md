# Agent templates (verbatim Hub export)

This directory holds the 13 Scion agent templates that run osprey-core, exported verbatim from the
`osprey-hub` Hub (project `862ead69-1199-47d0-ba6c-16084cef482b`, API `/api/v1/templates`).

Each `<name>/` directory contains:

- `template.json`: sanitized Hub metadata (id, name, slug, displayName, description, harness,
  defaultHarnessConfig, agentConfig, scope, scopeId when present, contentHash, status, created,
  updated, files, source). Per-user and storage fields are dropped (updatedBy, _capabilities,
  storageUri, storageBucket, storagePath). `files[].mode` is `null` when the Hub reports no mode.
- `scion-agent.yaml`, `agents.md`, `system-prompt.md`: the template files, byte for byte.

`MANIFEST.json` lists the 13 templates sorted by name: id, scope, harness, model, contentHash, and
the sha256 and size of every file.

## The 13 templates

Harness and model are the Hub metadata values (`harness`, `agentConfig.model`). Four templates
carry a `scion-agent.yaml` that declares something else; see "Metadata versus file differences".
Stages are the ones each core-* `agents.md` was authored with; the canonical id follows in
brackets where it differs (see "Stage names" below).

Project-scoped templates (scope `project`, scopeId = the osprey-core project; files served by the
Hub API):

- `core-coordinator`: routes S1_SPEC..S6_DEPLOY_CLOSE, delegates to the sub-coordinators, holds the
  council gate, never implements. Harness `antigravity`, model `Gemini 4 Argon (High)`. Stages: all
  six.
- `core-sub-coordinator`: manages one disjoint specialist tree and verifies hand-backs. Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S1_SPEC, S2_CODE, S3_TEST, S4_REVIEW
  [S1_SPEC, S2_TESTS, S3_BUILD, S4_VERIFY_QA].
- `core-planner`: work-package decomposition, dependency graph, acceptance mapping. Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S1_SPEC.
- `core-spec-writer`: API contracts, config schemas, numbered acceptance criteria. Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S1_SPEC.
- `core-builder`: implements the spec at <= 400 lines per file, with tests and measured latencies.
  Harness `antigravity`, model `Gemini 4 Argon (High)`. Stages: S2_CODE [S3_BUILD].
- `core-tester`: pytest triad (happy path, edge cases, chaos) with evidence. Harness `antigravity`,
  model `Gemini 4 Argon (High)`. Stages: S3_TEST [S2_TESTS + S3_BUILD].
- `core-reviewer`: independent council review (Gemini / Claude Opus / Claude Fable). Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S4_REVIEW [S4_VERIFY_QA].
- `core-security-auditor`: IAP posture, container hardening, secret hygiene, least-privilege IAM.
  Harness `antigravity`, model `Gemini 4 Argon (High)`. Stages: S4_REVIEW, S5_RELEASE_GATE
  [S4_VERIFY_QA, S5_RELEASE_GATE].
- `core-docs-writer`: runbook, API reference, release notes and the S6 close packet. Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S4_REVIEW, S6_DEPLOY_CLOSE
  [S4_VERIFY_QA, S6_DEPLOY_CLOSE].
- `core-deployer`: deterministic release-gate checks and the Cloud Run rollout behind IAP. Harness
  `antigravity`, model `Gemini 4 Argon (High)`. Stages: S5_RELEASE_GATE, S6_DEPLOY_CLOSE.

Global templates (scope `global`; files read from the Hub blob store), the 3-model QA council:

- `qa-gemini`: QA council member. Harness `antigravity`, model `Gemini 4 Argon (High)`. Stage:
  S4_VERIFY_QA (authored as S4_REVIEW in the core-* texts).
- `qa-opus`: QA council member. Harness `claude`, model `claude-opus-5-5@default`. Stage:
  S4_VERIFY_QA.
- `qa-fable`: QA council member. Harness `claude`, model `claude-fable-5-1@default`. Stage:
  S4_VERIFY_QA.

## The 3-model QA council

`qa-gemini`, `qa-opus` and `qa-fable` give three independent verdicts from three model families.
The core-coordinator text holds the gate: all three must PASS before S5_RELEASE_GATE opens.

- The three templates share the same three files (identical sha256): the generic Factory Osprey
  `qa` role, which verifies acceptance criteria end to end, records evidence and never fixes
  product code.
- The per-template harness and model live in the Hub metadata (`harness`, `agentConfig.model`,
  and `agentConfig.env.ANTHROPIC_MODEL` for the claude members), not in the shared yaml (see
  "Metadata versus file differences").
- The Hub files endpoint returns 404 for these global templates, so their files come from the
  Hub's content-addressed blob store (`templates/global/qa.blobs/<sha256>`), each checked against
  the metadata hash and size.
- `contentHash` differs across the three although the files are identical: the Hub hash covers
  more than the file bytes. It is recorded verbatim, not recomputed.

## Metadata versus file differences

The export keeps both sides verbatim, so these differences are visible in the tree:

- `core-tester` and `core-security-auditor`: metadata says harness `antigravity`, model
  `Gemini 4 Argon (High)`; their `scion-agent.yaml` declares `default_harness_config: claude`
  and `model: "claude-opus-5-5@default"`.
- `qa-gemini` and `qa-fable`: the shared `scion-agent.yaml` declares harness `claude` and model
  `claude-opus-5-5@default`; the metadata carries their own harness and model (above).
- The other nine templates agree: yaml harness and model equal the metadata values.

The tests pin this exact set, so a Hub-side fix shows up as a test failure at the next export and
this section is updated with it.

## How to verify

    pytest tests/test_templates_export.py

The checks run offline: exactly these 13 directories; each holds template.json, every listed file
and nothing else; every file's sha256 and size match template.json; MANIFEST.json matches the
directories, ids, hashes and count 13; the council harness and model pins; core-* project scope;
the metadata versus file differences above; the earlier stage names in the core-* texts; no secret
patterns (private key headers, token prefixes, email addresses, Cloud Run hosts, standalone 10+
digit numbers such as project numbers); canonical JSON; and fault injection that proves the
verifier catches a flipped byte, a missing or extra file, and a manifest mismatch.

Spot check by hand: `sha256sum templates/core-builder/agents.md` prints the `sha256` that
template.json and MANIFEST.json list for that file.

## Provenance

- Verbatim: file bytes are never edited. At export time sha256(bytes) equalled the Hub metadata
  hash and the byte count equalled the metadata size for all 39 files; any mismatch aborts the
  export before anything is written.
- The exporter is GET-only (Hub API reads, `gcloud storage cat` for blobs) and lives with the Hub
  tooling outside this repository. It is deterministic: two runs differ only in
  `MANIFEST.exported_at`.
- A re-export replaces the 13 directories and MANIFEST.json. This README is maintained by hand.

## Stage names

The core-* `agents.md` texts were authored with the earlier stage names S2_CODE / S3_TEST /
S4_REVIEW. The canonical workflow ids are S1_SPEC, S2_TESTS, S3_BUILD, S4_VERIFY_QA,
S5_RELEASE_GATE, S6_DEPLOY_CLOSE. Map:

- S2_CODE -> S3_BUILD
- S3_TEST -> S2_TESTS + S3_BUILD
- S4_REVIEW -> S4_VERIFY_QA

S1_SPEC, S5_RELEASE_GATE and S6_DEPLOY_CLOSE keep their names. The template texts stay verbatim;
apply the map when reading them.
