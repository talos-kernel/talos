# Talos 0.11 — Claude Worker (`delegate_code`) Design

Date: 2026-08-27 · Branch: `feature/claude-worker` · Target release: **0.11.0**

Scope decided with the operator on 2026-08-27: Claude Code becomes a **persistent
worker daemon** that Talos delegates bounded coding jobs to. Rollout: generic
worker in the public tree **and** live activation on the Pi deployment, proven by
two real end-to-end runs before release.

## Doctrine (unchanged, governs every section)

- The model proposes, the kernel decides. The worker is an **executor**, never a
  reasoner: Talos' reasoner emits a `delegate_code` tool call, `policy.decide()`
  rules on it, the worker performs. No code path lets the model reach the worker
  socket directly.
- Kernel files stay semantically untouched; additions happen beside the kernel.
- Public hygiene: worker, unit, docs and tests ship neutral in
  `talos-kernel/talos`. Private hosts, tokens, aliases and runtime config stay
  private. Public state is only ever produced by `scripts/sync-public.sh`.
- Model-configuration invariance (operator rule, vault-recorded): the worker
  changes **transport and isolation only**. Model choice, provider routing,
  picker entries, fallbacks and credentials of the deployment stay bit-identical;
  pre/post deploy snapshots must match or the deploy stops.

## The honest tension, stated up front

CLAUDE.md says *"Never let a delegated run write"* — `subagent.ReadOnlyCeiling`
turns everything except reading into `DENY`, because a subagent is born from
model text and must be able to do *less* than its caller.

`delegate_code` deliberately creates a new effect class beside that rule, and
this spec exists to make the difference exact rather than to quietly violate it:

- The read-only ceiling governs **subagent runs that execute Talos tools inside
  the agent loop**. The Claude worker gets **no Talos tools**. It is one gated
  action at submission time — the same trust shape as `run_shell`: the kernel
  judges the *delegation*, and OS-level confinement bounds everything that
  happens inside, because per-action gating of a foreign agent's internal loop
  is not possible and pretending otherwise would be the real violation.
- The job's working directory is **derived by the kernel, never taken from the
  arguments** (`policy.claude_job_workspace(job_id)` — the
  `policy.frame_output_path` pattern). The model cannot pick where a foreign
  agent's bytes land.
- Effect class: same as `run_shell`. The autonomy dial and channel ceilings
  apply unchanged — they can only tighten. There is no second source of
  permission, and no "approve the worker once" path.

What this makes possible that was not possible before: a model-approved turn
can cause files to be written **inside a fresh, kernel-derived, disposable
workspace** by Claude Code, with network access to Anthropic's API. Nothing
outside that workspace is writable by the job.

## 1. Worker daemon (`talos/claudeworker.py`, new)

- systemd **user** unit template `deploy/talos-claude-worker.service`
  (documentation-grade, like the model worker unit). Unix socket at
  `$XDG_RUNTIME_DIR/talos/claude-worker.sock`, mode 0600, bearer token from
  `TALOS_CLAUDE_WORKER_TOKEN` (schema class SECRET).
- Protocol: newline-delimited JSON over the socket. `submit {prompt, job_id}`
  → streamed events `started | output | done | failed | timeout`. Socket reads
  carry a read timeout **and** every job carries an overall deadline (default
  15 min) — the two anti-trickle hardenings from the agent_consult review,
  applied from day one.
- Per job the worker spawns `claude -p --output-format stream-json` with:
  - `cwd` = `policy.claude_job_workspace(job_id)` — a fresh directory under
    the configured worker root; the job never sees another job's directory.
  - `--allowedTools` restricted to file + shell tools; **never**
    `--dangerously-skip-permissions`.
  - Child environment reduced to an allowlist: `PATH`, `HOME` (a dedicated
    worker home holding *only* the Claude OAuth state), locale. **No Talos
    secrets, no bridge token, no deployment env** — the child-env isolation
    hardening, proven by a test that inspects the actual spawn environment.
- Sandboxing: the job runs under the same confinement mechanism as `run_shell`
  (bubblewrap on Linux, `sandbox-exec` on macOS) with **one documented
  difference**: network stays on, because the job is meaningless without the
  Anthropic API. Root stays read-only; only the job workspace is writable.
  Where no confinement is available the worker **refuses** the job (same
  fail-closed rule as `run_shell`; `TALOS_SANDBOX_ALLOW_UNCONFINED` does not
  apply to worker jobs — an unconfined foreign agent is not a degradation, it
  is a different product).
- Limits: max 2 concurrent jobs, prompt length cap, per-job output cap
  (bytes), overall deadline. Overflow is a `failed` event with a reason, never
  a silent truncate.
- Evidence: the worker parses Claude's `stream-json` and appends structured
  job events to the Talos event log under the originating `run_id`. Status and
  results are read **from the event log / worker stream only — never from the
  model's prose** (the consult-evidence hardening).

## 2. Talos-side tools (2 new, both gated)

- `delegate_code {prompt}` → submits a job, returns `{job_id, workspace}`.
  `TARGET_EXTRACTORS` derives the target from the kernel-computed workspace
  path; a tool without an extractor is `DENY` by construction, so the extractor
  lands in the same commit as the tool.
- `delegate_status {job_id}` → reads the job's state from the event log /
  worker (READ effect).
- Live tracking: worker events flow through the existing `CliActivity` /
  `talos events --follow` path with their own glyphs (`ux.py` only, one
  meaning each).
- Wiring follows the manifest discipline of CLAUDE.md trap §7: runner map,
  manifest, and a `--once` smoke run after wiring.

## 3. Configuration (schema.py, all default-off)

| Key | Class | Purpose |
|---|---|---|
| `TALOS_CLAUDE_WORKER_ENABLED` | POLICY | master switch, default off |
| `TALOS_CLAUDE_WORKER_SOCKET` | POLICY | socket path override |
| `TALOS_CLAUDE_WORKER_ROOT` | POLICY | worker root for job workspaces |
| `TALOS_CLAUDE_WORKER_TOKEN` | SECRET | socket bearer token |
| `TALOS_CLAUDE_WORKER_MAX_PARALLEL` | SETTING | default 2 |
| `TALOS_CLAUDE_WORKER_JOB_TIMEOUT` | SETTING | overall deadline, default 900 s |
| `TALOS_CLAUDE_WORKER_BIN` | POLICY | claude binary path (pinning, not choice) |

`config set` refuses the POLICY/SECRET ones as always. `.env.example` ships
them neutral and commented.

## 4. Red-team cases (new, mandatory beside the loosening)

1. Child-env isolation: spawned job environment contains no Talos secret and
   no bridge token (asserted against the real spawn call).
2. Trickle defence: a worker that drips bytes forever hits the overall job
   deadline; a stalled socket hits the read timeout.
3. Evidence spoofing: a job result claimed only in model prose does not
   appear as a completed job; status answers come from the event log.
4. Socket auth: submit without/with wrong token is refused; socket file mode
   is 0600.
5. Path escape: a prompt asking Claude to write outside its workspace fails
   at the confinement layer (runs for real in `test_sandbox`-style, skips
   where the platform has no implementation).
6. Prompt injection through `delegate_code`: instructions embedded in the
   delegated prompt cannot reach Talos tools, the socket token, or the
   deployment env.
7. Unconfined refusal: with no sandbox available the job is refused, not run.

## 5. Website consolidation (talos-agent.ch)

Measured problem: the same product is presented three times — `index.html`
(Kernel/Tools/Nail/Proof/Install), `dossier.html` (kernel/ledger/myth/tools/
limits/install) and `console.html` — and the copies already drift
(`dossier.html` claims "The instruments 18" while the product has 19).

- `index.html` becomes the **single** marketing structure; dossier's unique
  content (ledger specimens, name provenance, known limits) moves into index
  as anchor sections; `dossier.html` is removed and `.htaccess` redirects it.
- `console.html` stays as the interactive demo (distinct purpose, no prose
  duplication).
- `tests/test_site_claims.py` is extended to guard **every** remaining page,
  so two structures can never drift apart again — one structure, one guard.
- The new worker lands in the same pass: tools 19 → **21**, new test counts
  in README (incl. badges, URL-decoded), CLAUDE.md, all site pages.

## 6. Verification chain (hard gate, in order)

1. TDD per change; each fixed target test runs **twice**, then the full suite
   once (`.venv/bin/python` — the global python3.13 shadows `tests`).
2. `redteam.py` twice green.
3. `scripts/check-public-hygiene.py` green on private **and** public tree.
4. Public sync via `scripts/sync-public.sh` into the separate clone only.
5. Model-config snapshot on the Pi **before** deploy; deploy via
   `scripts/deploy-pi.sh` with read-back; snapshot **after** deploy; any
   difference = stop/rollback.
6. Worker unit installed and enabled on the Pi; **two real `delegate_code`
   E2Es** (Claude makes a small change in a test workspace, verified by
   read-back from the event log and the filesystem); credential-isolation
   check of the running worker process.
7. Parity documented in the vault (private/public/runtime SHAs, test counts,
   backup paths). Release tarball only from the final commit; signing stays
   paused for the operator's offline key as before.

## 7. Explicitly out of scope

- No change to the deployment's model, provider, routing, picker, fallbacks
  or credentials (invariance rule).
- No inbound network surface: the socket is local-only, the worker fetches
  nothing on a stranger's say-so.
- No persistent memory for Claude jobs: each job is disposable; continuity
  lives in Talos' event log, not in the worker.
- No Cline/other-agent backend in this release — the worker binary is pinned
  by config (`TALOS_CLAUDE_WORKER_BIN`), swap is a later, separate decision.
