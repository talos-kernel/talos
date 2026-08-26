# Talos 0.10 — Full Upgrade Design

Date: 2026-08-26 · Branch: `upgrade/0.10` · Target release: **0.10.0**

Source of truth for scope: an internal comparison report of 2026-08-19
(8 prioritized items) minus what 0.9.x already shipped (provider fallback, `anchor --send`,
`talos health`, outcome failure note).

## Doctrine (unchanged, governs every section)

- The model proposes, the kernel decides. Nothing in this upgrade weakens that order.
- Feature parity with Hermes/OpenClaw is **not** the goal. The goal is Betriebsreife
  (ops maturity) plus the last architectural hardening (UID split). The comparison page
  states this openly.
- Kernel files (`policy.py`, `capability.py`, `command_floor.py`, `approval.py`,
  `standing.py`, `autonomy.py`, `trust.py`, `verifier.py`, `executor.py`,
  `schedule.py`, `plan.py`, `subagent.py`) stay semantically untouched. Additions
  happen beside the kernel, never inside its decision path.
- Public hygiene: nothing private (paths, names, addresses, keys, history) crosses
  into `talos-kernel/talos`. Public state is only ever produced by
  `scripts/sync-public.sh`.

## 1. Ops tooling (new, isolated files)

### `scripts/deploy-pi.sh`
- rsync deploy of the **package only** (`talos/` → `talos/`), never the repo root
  (deployment keeps its own SOUL.md/CLAUDE.md/env/data — CLAUDE.md trap §10).
- Guards: refuse empty/"/" targets; refuse targets whose SOUL.md first heading does
  not match the expected deployment name; require `--target user@host:path` form.
- Pre-deploy: run `python -m pytest tests/ -q` locally unless `--skip-tests`.
- Post-deploy: diff report of Pi-only lines (env, SOUL.md untouched), optional
  `--commit` creates a local commit on the Pi; `--rollback` restores the previous
  package tree kept at `<target>.bak-<timestamp>`.
- Dry-run by default; `--apply` executes.

### `scripts/backup-data.sh`
- Off-device backup of the deployment's `data/` (event log, entities, schedules,
  anchors): timestamped `tar.gz` pulled over SSH to a configurable destination,
  optional age encryption when `age` recipient is configured, plaintext refused
  unless `--insecure-plain` is passed. Rotation keeps last N (default 14).

## 2. Scheduling layer

- New module `talos/briefing.py`: morning briefing = health status + broken-chain
  alarm + pending approvals + yesterday's failure count + anchor age, sent to the
  owner's Telegram chat (same recipient resolution as `anchor --send`). Built from
  persistent sources only; a broken log makes the briefing say so, never silent.
- Wired as a schedule entry the operator can install via `talos briefing --install`
  (writes into the schedule DB through the existing `schedule.py` path — scheduled
  tasks keep `UnattendedCeiling`; the briefing is a send-only task, no tool use).
- `anchor --send` gains `--mail` (deliver digest via the configured IMAP/SMTP sender
  in addition to Telegram). Mail send path reuses the existing mail module's
  allowlist; no new inbound surface.
- `deploy/` gets systemd timer templates (`talos-briefing.timer`,
  `talos-anchor.timer`) as documentation-grade examples.

## 3. Outcome counter (report item 7)

- `outcome.py` gains a totals line: every final answer to a run that used tools
  carries "N tool calls, M failed" where N counts all tool calls of the run from the
  event log by `run_id` and M counts failures not later succeeded. Existing detail
  list (up to 3) unchanged. Fail-open preserved: broken log → no line, never a
  wrong number. Tests updated in `tests/test_outcome.py`; red-team strings asserted
  there and in `redteam.py` adjusted in the same commit (trap §2).

## 4. UID separation of the model worker (report item 4)

**Goal:** the agent process never holds a provider credential. API keys move to a
dedicated OS user (`talos-model`); reasoning calls cross a Unix socket.

- New module `talos/modelworker.py`: minimal daemon, JSON-lines over
  `/run/talos/model.sock` (dir root-owned 0750, socket 0660 `talos:talos-model`).
  Request: provider, model, messages, params. Response: text, or classified error
  kind (same `ReasonerFailure` taxonomy as `api_reasoner.py`, so the fallback chain
  works unchanged across the socket).
- Worker config: `/etc/talos/model.env` owned `talos-model` mode 0600 carries keys
  and base URLs. The agent's own env schema marks API keys as *worker-scope* when
  worker mode is on; `config get` keeps answering `[REDACTED]` either way.
- `ApiReasoner` gets a transport seam: direct HTTP (today, default) or socket
  (`TALOS_MODEL_WORKER=socket:///run/talos/model.sock`). Default stays direct —
  dev machines and macOS are unaffected; the split is an install-time choice.
- Fail-closed: unreachable socket is a classified `network` failure (fallback chain
  applies); it never falls back to reading a key from the agent's own env.
- Worker has no tool code, no filesystem access beyond its own env, no shell.
  OAuth/CLI reasoners (subscription logins that spawn a CLI) cannot move into the
  worker — documented limit, same honesty as the fallback chain's.
- systemd units: `talos-model.service` (User=talos-model, hardening directives) +
  install notes in `docs/`.
- Tests: protocol round-trip against a local socket, error-kind mapping,
  malformed-frame robustness (a garbage frame cannot crash or wedge the main
  loop), schema redaction in worker mode, and a red-team case proving the agent
  user cannot read `/etc/talos/model.env`.

## 5. Site (talos-agent.ch)

- New page `site/vergleich/index.html` ("Talos vs. OpenClaw vs. Hermes"): honest
  comparison from that report — security architecture, sandbox, audit chain,
  honesty features vs. their feature breadth; explicitly frames the gap as
  doctrine. Linked from `index.html` nav and `docs/`.
- `sitemap.xml` lists all pages (currently 2 of 5).
- `scripts/deploy-site.sh`: rsync of `site/` to the web host with the same guard
  discipline as deploy-pi (dry-run default, target guard).
- No trackers, no external JS beyond what exists; the kernel demo (`kernel.js`)
  may be reused on the comparison page for the "the model proposes" visual.

## 6. Release & deployment

- Version 0.10.0, CHANGELOG in Keep-a-Changelog style, signed release per
  `SECURITY.md` process.
- `pytest` (1641) + `redteam.py` green before every merge to the branch head;
  `python -m talos --once` smoke after any wiring change (trap §7). `e2e.py` runs
  where a real model is configured; skipped with a note otherwise.
- Public sync via `scripts/sync-public.sh`, then a hygiene pass (grep for private
  hostnames, user names, mail addresses, absolute home paths in the synced tree).
- The operator's own installation via the new `deploy-pi.sh` (package-only sync),
  then `talos health` + `talos anchor --send` there as the read-back proof.
- Internal docs update: refresh the comparison report's DONE/OPEN status.

## Out of scope (explicit)

Browser clicking/typing, inbound webhooks, WhatsApp inbound, Hermes-style skill
breadth, YOLO modes. These are doctrine exclusions, not backlog.
