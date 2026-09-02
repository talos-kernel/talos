# Changelog

Notable changes per release. Security changes come first in each section and say what
they make possible that was not possible before — or, more often, what they take away.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are alpha: the kernel's rules are stable, the surface around them is not.

## [0.17.2-alpha] — 2026-09-02

### Security

- **Dependencies are pinned and hashed; the installer, the updater and CI
  refuse anything else.** `requirements.txt` named ranges (`requests>=2.31`),
  so the signature over the archive proved the archive — and then `pip
  install` fetched whatever PyPI offered at that moment, unverified (CWE-494;
  raised in the review of the `llmman launch talos` integration). Every
  release now ships `requirements.lock` and `requirements-dev.lock`: each
  version fixed, each file with its SHA-256, resolved for every platform when
  the release was cut. `install.sh`, `talos update` and the workflow install
  with `--require-hashes` — a package that does not arrive as released stops
  the install, and a tree that only names ranges is not installed at all. The
  installer no longer upgrades pip first, which was one unpinned download
  ahead of all the pinned ones. The `requirements*.txt` files stay as the
  statement of intent the locks are resolved from.

## [0.17.1-alpha] — 2026-09-02

### Fixed

- **A machine without Hermes could not start Talos at all.** The model
  catalogue was built from Hermes's own helper files, and a missing file was
  a hard stop (`catalog helper not found`) before the first model call —
  measured on 2026-09-02 against a fresh install from the published tarball,
  which is exactly what `ollama launch talos` and `llmman launch talos` hand
  a stranger. The built-in ways (`claude-cli`, `anthropic-api`, `openai-api`)
  were always meant to remain without Hermes; they never got their turn. The
  Hermes catalogue is now what it is, an addition: absent on the default
  paths, it costs the Hermes models and leaves `catalog.hermes_absent` in the
  log — never the start. Only a catalogue file the operator named
  (`TALOS_HERMES_PROVIDER_CATALOG`, `TALOS_HERMES_MODELS`) still has to exist.
- **The configured local provider is known even when no catalogue lists it.**
  `TALOS_MODEL_PROVIDER=ollama` (or `lm-studio`, `custom`) was "unknown
  provider" unless a Hermes catalogue happened to carry it. The provider now
  enters the catalogue with the one model the operator named; the rest of a
  local server's models still come from `talos models --refresh`. A known name
  grants nothing — every call passes the same kernel.
- **The readiness probe of a local provider asks the server, not the model.**
  `validate()` used to ask every provider to answer `TALOS_READY`; a small
  reasoning model behind `llmman serve` spent 3000 tokens thinking about that
  and hit the 180 s timeout with address and model both correct. A local
  provider has no key to prove, so the probe now reads `GET /models` (the
  OpenAI form Ollama, LM Studio, llama.cpp and llmman all speak) and checks the
  configured model is listed — no generated token. Anything else (no list, no
  JSON, model absent, a transport without `get`) falls back to the marker
  probe; never a silent yes.

## [0.17.0-alpha] — 2026-09-01

Four packages measured against Hermes v0.21 — the one release where another agent
was genuinely ahead in things an operator feels every day. Each one adds reach and
none adds a right.

### Added

- **A global stop that is only ever a stop: `/stopall` (also `/estop`).** One
  command aborts the running thought, drains the queue, detaches every
  `/background` task at its next step boundary (the model call itself is a
  blocking subprocess and stays untouchable; its report is discarded, logged as
  `conductor.reply_discarded`) and **discards** every pending approval — it never
  approves one. `approval.discard_all` is the only kernel touch and points in
  the DENY direction. Timed runs stay: a stop that deleted timers would create
  the next incident while ending this one. The reply is an honest balance per
  category; a second `/stopall` says "nothing left". Conductor and command
  centre now share one `BackgroundDesk` — two instances were two truths about
  what is running.
- **Timed runs with a memory and a probe.** Blueprints accept `continuity`,
  `monitor` and `probe`. With `continuity` the previous result is placed in front
  of the task framed as data (« », never an instruction), and a run that ends in
  the same failure as its predecessor is logged (`schedule.error_repeated`)
  instead of repeated into the chat — the key comes from `outcome.failed_tools`
  over the event log, never from prose. With `monitor` and a `probe` the probe is
  read *before* the model is asked; an unchanged output means no model call
  (`schedule.skipped_unchanged`). The probe is an ordinary `run_shell` of the
  task's principal through the same executor, kernel and sandbox under the same
  unattended ceiling — `continuity.py` imports neither `PolicyKernel` nor
  `sandbox`, so it cannot build a second way. A probe that fails, whatever the
  reason, fires the run: a broken sensor must not be the way to silence a
  watcher. `Conductor.handle(before_reply=)` can only withhold a reply, never
  grant anything; a hook that raises delivers.
- **`delegate_steer` — the 28th tool: a course correction into a *running*
  `/background` task.** The instruction is queued on the background desk
  (three pending at most, 400 characters like a delegated question) and read at
  the same step boundary where a typed correction enters a foreground run — one
  seam, a second mailbox. It arrives as a framed turn ("worded by the relaying
  run, no additional rights"); every tool call it provokes passes the kernel
  under the task's own ceiling, so `NEEDS_HUMAN` stays `DENY` in the background.
  Only the person and conversation that started the task may steer it, taken
  from the thread context and never from the arguments. Under the delegated
  ceiling the tool is `DENY` (`subagent.NOT_FOR_DELEGATES`): a delegate must be
  able to do less than its caller, and the run it would steer may write. Only
  `/background` runs are steerable — the only runs with a step boundary in this
  process; a synchronous `delegate`, a worker job or a timed run answers with a
  refusal, never a pretend "ok". Two red-team cases hold both directions
  (206 → 208). `policy.py` gains six lines: an extractor that says `task_id` is
  not a path.
- **`TALOS_MODEL_OVERRIDES` — the operator corrects what the catalogue does not
  know about a model.** One JSON object keyed by model id; the field list is
  closed (`context_window`, `input_price`, `output_price`, `vision`,
  `reasoning`) and contains neither a provider, an address nor a key — a value
  that could change the *way* to a model would be policy, and `schema.py` files
  the key as `SETTING` for exactly that reason. Broken JSON stops the start and
  names the variable, never the value; an unknown model id or field is dropped
  with `model.override_dropped` in the log — an override never adds a model.
  `talos models`, `/usage` and `/status` mark overridden values as the
  operator's word. Honest limit: there is no token-based context cap yet; the
  window shows in the display, it is not a bound.
- **`/status` says more — and only what is measured.** Session usage and the
  latency of the last model call (from `usage.py`), pending approvals across all
  chats, running background tasks, the next three timed runs, active model
  overrides. Where a source has nothing, the line is absent rather than invented.

### Changed

- The numbers the site and the README state: 2321 tests, 208 adversarial cases,
  `policy.py` at 895 lines, 28 tools (16 read, 5 write, 7 exec).

## [0.16.0-alpha] — 2026-08-31

### Added

- **A second backend for the confined delegate worker: Google's Antigravity
  CLI (`agy`) — and the 27th tool, `delegate_agy`.** One daemon, one socket:
  the submit frame gains an optional `"backend"` field (absent or `"claude"`
  is byte-for-byte the old behaviour; anything unknown is a named
  `invalid_request`, no spawn). agy jobs run the measured invocation
  (`agy -p --output-format stream-json --dangerously-skip-permissions
  --print-timeout <deadline>s`) under the same sandbox backends, the same env
  allowlist, the same kernel-derived disposable workspace and the same
  process-group deadline; `unconfined` stays filtered out. Two gates, as
  everywhere: the worker needs `TALOS_CLAUDE_WORKER_AGY_BIN` +
  `TALOS_CLAUDE_WORKER_AGY_HOME` (missing either → named `unavailable`, no
  spawn), the agent needs `TALOS_AGY_BACKEND=1` (without it `delegate_agy`
  is not even in the manifest — the MCP two-gate pattern). MCP/browser stay
  claude-backend only: an agy frame carrying them is `invalid_request`.
- **The agy credential model, honestly different.** agy knows no env token,
  so the token *file* has to travel: the worker copies
  `antigravity-oauth-token` from the worker's agy home into the disposable
  job home (mode 0600) before spawn, and the source path appears in neither
  the job env nor argv. A missing token is `unavailable`, named. The
  exfiltration risk is equivalent to the claude backend — the job has
  network either way — and the copy dies with the workspace.
- **The stream outranks the exit code.** agy's auth failure arrives as a
  `result` event with `status: "ERROR"` **and RC 0** — measured. A `result`
  event carrying a non-empty `error` or a status outside the tolerated OK
  set now fails the job regardless of the return code. agy events are read
  tolerantly (`event` vs `type`, summary from `.result.response`, file
  evidence from both event shapes, workspace-jailed as before); where
  nothing matches, `files` stays empty — evidence may be absent, never
  fabricated. `delegate_status` answers both backends and names the job's
  `backend` (default `"claude"`).

### Changed

- Test count: 2166 (was 2148). Adversarial cases: 206 (was 199). Tools: 27
  (was 26). Gate path: 782 lines (was 779).

## [0.15.1-alpha] — 2026-08-28

### Changed

- The dashboard (`talos dashboard`) wears the public site's design: the same
  oklch tokens, VT323 + Sometype Mono and the sigil — embedded as base64, so the
  page still loads nothing from anywhere. Raw JSON blocks are gone; status,
  runs, approvals, schedules and the event log render as structured cards,
  tables and verdict pills, still refreshed every five seconds, still GET-only,
  still without any button that could answer an approval. Data is rendered via
  `textContent` only.

## [0.15.0-alpha] — 2026-08-28

### Added

- **WhatsApp over an operator-controlled broker** (`talos/wabroker.py`). A second
  implementation of the `whatsapp` channel that actually receives: a listener on
  the operator's own broker host appends prefix-routed messages to a JSONL queue,
  and Talos *pulls* it over SSH with a persisted byte cursor — no webhook, no
  listening socket, the outbound-only posture holds. Outbound goes back through
  the broker's send script (base64-safe text, files via `scp` + broker). The
  sender number comes out of the operator's own WhatsApp account, so the channel
  carries `Trust.FULL` — as a property without a setter. First run starts at
  end-of-file (no backlog replay); a failed poll raises `channel.error` and the
  cursor never advances. Opt-in via `TALOS_WA_BROKER_SSH` (empty default = off;
  `TALOS_WA_BROKER_QUEUE` / `TALOS_WA_BROKER_CLI_DIR` for the paths). Configured,
  the broker wins over the Cloud-API variant — the registry demands unique
  names, and this is the only one of the two that fetches. Ships
  `docs/whatsapp-broker.md`.
- **`skill_write`: the one gated write path into the skills directory** (23rd
  tool, `talos/skillwrite.py`). The agent may *create* a skill — never modify,
  never overwrite (atomic `O_EXCL`), and never unattended: the tool is declared
  irreversible, so the kernel answers `NEEDS_HUMAN` without exception and the
  unattended ceiling turns that into `DENY`. Four hard refusals, all
  fail-closed with an honest reason: no overwrite, no `allowed-tools` anywhere
  (a second permission source next to the kernel), nothing that looks like a
  credential (same `looks_secret` as the long-term memory), no path escape
  (slug name, realpath-checked, symlink targets refused). The destination is
  derived by the kernel (`policy.skill_write_path`), never taken from model
  arguments.
- **Skill-distillation blueprint** (`blueprints/skill-distillation.json`,
  "every sunday 20:15"). The week's event log becomes at most one skill
  candidate — attempted through `skill_write`, parked as a vault note under
  `patterns/` when the unattended run is refused, and reported either way.
- **The MCP registry, documented publicly** (`site/registry/`,
  `examples/mcp-servers.json`): the operator-owned model behind
  `data/mcp-servers.json` — no marketplace, no third-party code in the agent
  process, servers run only inside the UID-separated claude worker, and the
  double gate (registry file + `TALOS_CLAUDE_WORKER_MCP_SERVERS` allowlist)
  stands.

### Changed

- **Daily reflection verified live.** The `daily-reflection` blueprint ran
  against the real installation end-to-end — scheduled, executed under the
  unattended ceiling, reported — instead of only in the suite.
- Test count: 2063 (was 2005). Tools: 23 (was 22). Gate path: 645 lines
  (was 593).

## [0.14.0-alpha] — 2026-08-28

### Added

- **Operator-owned MCP registry for confined coding jobs.** `delegate_code`
  accepted exactly one hardwired MCP server (chrome-devtools); it now resolves
  server *names* against `data/mcp-servers.json` — an operator-owned file with
  a `version` field, fail-closed parsing, per-entry byte caps, absolute-command
  and no-`env` rules — gated twice as before (agent `TALOS_MCP_SERVERS` and
  worker `TALOS_CLAUDE_WORKER_MCP_SERVERS`; the browser switches keep implying
  chrome-devtools so existing installs do not break). Frames carry names only,
  never commands or args; the worker still writes the MCP config itself.
  `browser: true` remains as an alias. Three new red-team cases hold the gates.
- **`delegate_dag`: an acyclic graph of confined coding tasks.** The model
  declares up to eight nodes with `depends_on` edges; ready nodes fan out to
  the UID-separated worker in parallel, dependents fire when their parents
  reach a terminal state, a failed parent skips its children with the reason
  stated, and one honest final report lands in the chat — evidence from worker
  frames only. Cycles, unknown parents, duplicate ids and ungated MCP servers
  are named refusals before any frame is sent; unattended ceilings DENY the
  tool like `delegate_code`. Validated in `talos/dag.py`, queued through the
  existing completions ticker, three new red-team cases.
- **Live observer dashboard.** `talos dashboard` serves a self-contained page
  plus `/api/status|runs|approvals|events|schedules` on `127.0.0.1:8810` —
  read-only SQLite (`mode=ro` + `query_only`), redacted payloads, no schedule
  prompts, no POST ever (405), a separate observer process by doctrine, not a
  way in. Running runs and open approvals are log approximations and say so.
  Ships `deploy/talos-dashboard.service` and `docs/dashboard.md`.

## [0.13.1-alpha] — 2026-08-27

### Fixed

- **Raw `PLAN: {…}` JSON no longer leaks into the Telegram chat.** The reply
  stream muted `TOOL_CALL` lines as machinery but not the plan line, so a
  plan's raw JSON briefly became the message (screenshot from the operator).
  The plan's human form already exists — the activity line "🗺️ N steps — …" —
  and a plan-led turn is either all machinery or discarded and retried by the
  loop, so muting it loses nothing meant for the operator. Prose that merely
  starts alike ("PLANET …") still shows.

## [0.13.0-alpha] — 2026-08-27

### Security

- **Attended auto-approval only ever converts the kernel's own NEEDS_HUMAN,
  behind all four ceilings** (`talos/autonomy.py`). Unattended and delegated
  runs turn NEEDS_HUMAN into DENY before it can reach the conversion; channel
  trust below FULL is DENY; a lowered autonomy dial still asks. What this
  takes away: nothing the walls covered — system/secret paths are DENY before
  this point, and the routine class is derived from tool-spec properties
  (sandbox-contained or reversible, targets re-checked against the kernel's
  own floor functions), so an outward-send or credential-carrying tool can
  never fall into it, including tools that do not exist yet.
- **Every auto-approval is evidence, not silence**: `approval.auto_attended`
  is written to the hash-chained event log before any effect, and the grant
  carries `human_approved=false` — no fake "yes" anywhere in the chain.

### Changed

- **Attended is generous, unattended stays strict.** Interactive runs (a real
  message from an allowed principal) no longer prompt for the routine class:
  sandboxed shell, reversible reads, workspace writes with snapshot/undo.
  Scheduled runs, blueprints and delegated runs keep exactly the old verdicts
  (`TALOS_ATTENDED_AUTOAPPROVE=0` restores prompting everywhere).
- **The agent stopped reciting rules.** Protocol-level tone change: a denial
  is answered with one sentence (reason + legitimate path, then take it),
  operator corrections are one sentence with evidence, never an opener, and
  burned tool calls are not conversation — the evidence lives in /events.
  Substantial build/code/research tasks now consider `delegate_code` by
  default.
- **The site leads with strength.** New hero: "Strong enough to hand a shell
  to. Verifiable enough that you can." — the gate demo stays, reframed as
  "watch it work", plus a new section for the 0.12 capabilities (confined
  worker, completion push, MEDIA: attachments, blueprints, four channels).
- **Browser MCP proven live on the Pi.** `TALOS_CLAUDE_WORKER_BROWSER_CMD`
  runs a fixed installed chrome-devtools-mcp binary (the job's npm cache is
  read-only — `npx @latest` would re-download into every disposable HOME),
  and `TALOS_CLAUDE_WORKER_BROWSER_CHROME_ARGS` passes `--chromeArg` flags
  (Chrome under bubblewrap needs `--no-sandbox`: it may not create its own
  namespaces). Measured gotcha: `/usr/bin/chromium` is a shell wrapper
  sourcing `/etc/chromium.d/*` and dies in the masked sandbox — point
  `TALOS_CLAUDE_WORKER_BROWSER_CHROME` at the real binary
  (`/usr/lib/chromium/chromium`). E2E on the Pi: navigation, title read,
  file written, read-back verified.
- Test count: 1912 (was 1890).

## [0.12.0-alpha] — 2026-08-27

### Security

- **`MEDIA:` attachments are gated at the kernel floor.** A `MEDIA:` line in the
  agent's own reply sends a generated file as a real chat attachment — but only
  from kernel-derived roots (workspace, claude-jobs), never from system paths,
  never secret-shaped names (`.env`, `*.pem`, `*.key`, …) at any depth, capped
  at 20 MB and 4 files per reply. Tool output, web pages and operator text can
  never forge a tag: extraction runs on the agent's reply text only. What this
  takes away: a path the model names is no longer a path the model gets.
- **Browser automation stays behind the sandbox wall.** chrome-devtools-mcp
  runs as an MCP server *inside* the confined Claude worker job — never as a
  native Talos tool (a click has no derivable target, so it is DENY by
  construction). Double opt-in, both default off: agent-side
  `TALOS_BROWSER_MCP_ENABLED` and worker-side `TALOS_CLAUDE_WORKER_BROWSER_MCP`.
  A browser request against a disabled gate is refused by name, never silently
  run without the browser — the kernel's evidence would otherwise lie. The
  generated MCP config carries no `env` block: no secret crosses that frame
  (proven by an env-diff adversarial test).

### Added

- **Completion push for delegated jobs** (`talos/notify.py`). When a
  `delegate_code` job reaches a terminal state (`done`/`failed`/`timeout`),
  the origin chat gets a short factual message built from the worker record —
  never model prose. `TALOS_COMPLETION_PUSH=0` disables it.
- **Automation blueprints** (`talos/blueprints.py`). Installable, plain-language
  schedules over `schedule.py` — "every morning 08:30", "weekdays 18:00",
  "every 2 hours", never cron syntax. `/blueprints` lists,
  `/blueprint install|remove|enable|disable|status` manages. Installed
  blueprints are ordinary schedule entries: `UnattendedCeiling` applies
  unchanged, by construction.
- **`delegate_code` gains `browser: true`** — adds chrome-devtools-mcp to that
  job's sandbox (navigation, forms, extraction) when both gates are open.
- Test count: 1890 (was 1773).

## [0.11.0-alpha] — 2026-08-27

### Security

- **`requires_env` is now enforced by the kernel.** `ToolSpec.requires_env`
  was declared but consumed nowhere; a tool whose required environment was
  missing would simply run. `policy.decide` gains a step that DENYs such a
  request, naming the missing variables (never their values). What this takes
  away: an env-gated tool could previously execute without its gate.
- **Confined Claude Code delegation (`delegate_code`).** A persistent worker
  daemon (`talos/claudeworker.py`) speaks JSON-lines over a filesystem-
  permissioned Unix socket (0660 + group — no bearer token to leak) and runs
  each accepted job as `claude -p` under the existing sandbox backends:
  root read-only, only the job workspace writable, network on (the API is the
  job's purpose — the one documented difference from `run_shell`). The job
  workspace is derived by the kernel (`policy.claude_job_workspace`), never
  taken from model arguments. Child environments carry a positive allowlist
  only — no Talos secret, no bridge token. Jobs have an overall deadline
  beside the read timeout, are capped in parallel and output, and are
  **refused unconfined** (`TALOS_SANDBOX_ALLOW_UNCONFINED` does not apply).
  Evidence — summary, changed files — is parsed from the worker's
  `stream-json` only, never from model prose; paths claimed outside the
  workspace are dropped. What this makes possible: an allowed turn can cause
  files to be written inside a fresh, kernel-derived, disposable workspace by
  Claude Code. Default off; opt-in via `TALOS_CLAUDE_WORKER_*`
  (`docs/claude-worker.md`, `deploy/talos-claude-worker.service`).

### Fixed

- **DNS inside network-allowed sandboxes.** `/etc` is an empty tmpfs mask, so
  a job granted `--share-net` had no `resolv.conf` and failed with "Unable to
  connect to API" — measured on this release's first live E2E. The bubblewrap
  profile now re-binds `resolv.conf`/`nsswitch.conf`/`hosts` (realpath) when
  and only when the network is shared.
- **`ProtectKernelTunables` dropped from the worker unit** (incompatible with
  bubblewrap's own `/proc` mount — every job died silently; found by bisect)
  and **failed/timeout jobs now carry an `error` field** (stderr tail or
  spawn reason, capped at 2000 chars) plus the returncode through
  `delegate_status` — the first live run proved a silent `failed` is
  undebuggable.
- **The job's HOME moved into the job workspace** (`<workspace>/.home`):
  Claude needs a writable HOME for state, and the workspace is the only
  writable place — measured on the second live E2E, where Claude's Bash died
  on the read-only filesystem. The OAuth token is now read fresh per job by
  the daemon and passed as a `CLAUDE_CODE_OAUTH_TOKEN` value (the job's own
  credential); the token file never enters the sandbox.

### Added

- Two gated tools, 21 total: `delegate_code` (submit a bounded coding job;
  Effect.EXEC, `sandbox_required`) and `delegate_status` (read a job's state
  back; Effect.READ). Live tracking flows through the existing activity /
  `talos events --follow` path.
- Config: seven `TALOS_CLAUDE_WORKER_*` keys (POLICY/SETTING, default off).

### Changed

- Website consolidated into **one structure under one claims guard**:
  `dossier.html` was merged into `index.html` (anchored sections, 301
  redirect) after drifting a tool count behind; `tests/test_site_claims.py`
  now guards every remaining page for tests/adversarial/kernel-lines/tools.
- Suites: 1767 tests, 173 adversarial cases, 589-line gate path.

## [0.10.0-alpha] — 2026-08-26

### Security

- **UID separation for provider credentials (model worker).** A new daemon,
  `talos/modelworker.py`, speaks JSON-lines over a Unix socket and runs as its own
  OS user; API keys and base urls move to `/etc/talos/model.env` (0600, owned by
  that user). `ApiReasoner` gains a transport seam: direct (unchanged default) or
  socket via `TALOS_MODEL_WORKER`. Fail-closed on both sides: a dead socket is a
  classified `network_failed` — the fallback chain works unchanged across the
  socket — and the reasoner *never* silently falls back to a key from the agent's
  own env while worker mode is on; a test proves zero direct HTTP attempts in
  exactly that situation. Error kinds are the real `ReasonerFailure` taxonomy.
  What this takes away: a compromised agent process could previously read the
  provider keys. Honest limits in `docs/model-worker.md`: OAuth/CLI reasoners
  cannot move into the worker, and there is no streaming across the socket. The
  split is an install-time choice (`deploy/talos-model.service`), no setuid in
  code; `redteam.py` adds a host check that the agent user cannot read the
  credential file (honest SKIP where no worker is installed).

### Added

- **Morning briefing as a product.** `talos briefing` reports from persistent
  sources — chain state, failures in the last 24 h, pending approvals, anchor
  age — and says so explicitly when the log is broken, never silently.
  `talos briefing --install` registers it through the existing `schedule.py`
  path (`UnattendedCeiling`, no new rights); the model-free send path is the
  systemd timer pair in `deploy/`.
- `talos anchor --send --mail` additionally mails the chain digest to the mail
  principal in the allowlist.
- **Outcome totals line.** Every final answer of a run that used tools now
  carries "N tool calls, M failed", counted from the event log by `run_id` —
  the failure list alone had a hole: an invented success leaves no error event,
  only the number gives it away. Fail-open preserved; tool-free answers stay
  clean.
- `scripts/deploy-pi.sh`: guarded rsync deploy of the package only (SOUL.md,
  CLAUDE.md, `*.env`, `data/` stay deployment-owned), dry-run default, target
  guards, `--backup`/`--rollback`, post-deploy verification with zero remaining
  diff, tests run in the project venv beforehand.
- `scripts/backup-data.sh`: off-device backup of `data/` over SSH into a
  timestamped archive; refuses plaintext without an explicit `--insecure-plain`,
  encrypts with `age` otherwise, rotates with a sha256 receipt.
- `scripts/deploy-site.sh`: site deploy in the same guard discipline.
- **Comparison page** `site/vergleich/` — Talos vs. OpenClaw vs. Hermes, the
  honest version: kernel, sandbox, hash chain and report-vs-reality against
  their feature breadth, the gap framed as doctrine, not backlog. The sitemap
  now lists all six pages.

### Changed

- All pages, README badges and CLAUDE.md state the real numbers again:
  1680 tests, 164 adversarial cases, 529 kernel lines.

## [0.9.5-alpha] — 2026-08-20

### Added

- A runtime fallback chain for the model (`TALOS_MODEL_FALLBACKS`, comma-separated
  provider/model specs). When a run fails with a *classified* error — rejected key,
  quota, overload, network, timeout, never a factual 4xx — the run retries the next
  entry in order and every attempt lands in the audit trail as a
  `model.fallback.runtime` event: a silent fallback would be an invisible model change.
  The persisted model choice is never touched; the next run starts with the primary
  again. Local providers like `ollama` need no key; `nvidia-nim` and `kimi` join the
  catalogue with their own keys and base urls.
- `talos anchor [--send]` pins the event log's hash-chain head (head hash, entry
  count, timestamp) append-only in `data/anchors.jsonl` and alarms when a later run
  finds fewer entries than a previous anchor — the tail truncation a local chain
  cannot see, now caught with the head provable off this machine.
- `talos health [--json]` answers "is it well" in one command — runs and failures,
  the newest error entry, schedules and the anchor state — from sources already
  wired in. Exit 1 only on a broken chain.
- `talos events --since 4h` filters the audit trail by age (durations like `30m`, `4h`,
  `2d`), not just by tool, type or run — "what happened tonight" no longer needs manual
  database digging. An unreadable duration is refused, never guessed.
- `/health` answers the traffic-light question right in the chat: runs and failures from
  the usage meter, the newest error entry from the event log, queue state, pending
  approval, channels and dial — everything from sources already wired in. No network
  call, no write: a health display that had an effect of its own would not be a display.

### Fixed

- e2e: three real-model cases asserted on the model's wording and flaked on it —
  differently per run with identical code. They now check kernel facts: the standing
  rule, parked approvals, and the exec events (`Y2`, `Y3`, `Y7`). `Y3` prompts the
  replay with the command taken from the rule that was actually created, not a second
  hardcoded string, and a leftover parked approval from the resumed loop is cleared
  instead of eating the next case's input. `Z3` names the tool it means — tool
  *choice* is model behaviour, not a deterministic test subject — and proves the call
  happened via the event log. The wall-clock self-review is disabled in the harness:
  it delivered its report right after the answer into the same sink, and the harness
  read the report as the reply.
- The website was audited page by page against the code and corrected where it
  drifted: both interactive kernel demos now model the shipped default (sandboxed
  shell — clean commands run, the legacy every-command-asks mode was presented as
  product behaviour), the decision chains show the real orders including the
  identity step, and stale claims were replaced (environment variable names that
  exist nowhere, the pre-sandbox mail trust logic, a command table three entries
  short, invented glyph counts). `tests/test_site_claims.py` now guards the numbers
  on **every** page of the site, not just the landing page — the drift it caught
  immediately is the reason it exists.
- `SECURITY.md` describes the sandbox as it is (bubblewrap on Linux, `sandbox-exec`
  on macOS, fail-closed, the deliberate opt-out named); the README gains the
  first-run nuance for `cli:<uid>` and loses a subcommand count that had long
  drifted.

## [0.9.4-alpha] — 2026-08-12

### Fixed

- Hermes one-shot runs now keep `PLAN` and `TOOL_CALL` machine output in the final-answer
  channel that Talos can actually receive, instead of losing valid requests as commentary.
- Empty multi-word Vault searches now retry a bounded set of individual terms and merge
  the results by term coverage, while preserving the existing secret-path filters.
- Operator-owned entity knowledge now keeps similarly named agents and services distinct,
  and `entity_status` binds live checks to fixed configured HTTP or systemd sources rather
  than a model-supplied URL or unit.
- Every task now carries a bounded working-state block. Live factual answers get one
  deterministic self-correction round when their entity-specific evidence is missing;
  a second unsupported claim is delivered with an explicit `NOT VERIFIED` receipt.
- Hermes reasoning effort is routed per turn (`low`, `medium`, `high`), deep tasks receive
  Researcher/Operator/Reviewer choreography, and a versioned trace-evaluation pack covers
  entity-source confusion, Vault-backed infrastructure facts and non-invented-cost regressions.
- Lexical Vault misses now try a structured BM25+vector QMD query without expansion or
  reranking before falling back to bounded individual terms.

## [0.9.3-alpha] — 2026-08-07

### Security

- **The event log now proves it was not edited after the fact.** Each entry carries the
  SHA-256 of the one before it — a hash chain, the Merkle/Git idea (taken from ZeroClaw's
  "tool receipts", done differently: no per-action signature, whose key would sit on the
  same machine, but a chain). `talos verify` recomputes it and either reports the log intact
  or names the id of the **first** broken entry, exiting non-zero so a cron job or installer
  fails instead of trusting a tampered record. What it makes visible that was invisible
  before: an entry edited in place (its hash no longer matches), an entry deleted from the
  middle (the next one no longer finds its predecessor), and an entry blanked to pass as
  pre-chain (a NULL chain hash counts only as a leading legacy prefix, never mid-chain).
  - Migration is automatic. A log written before this version keeps a NULL chain and is
    reported as an *unproven prefix*, never silently counted as intact — "intact" over zero
    protected rows would be the half-truth this project exists to avoid.
  - Bounded honestly, and said so in `SECURITY.md`: local root can still recompute the
    *whole* chain, and cutting entries off the *end* leaves a shorter, self-consistent chain.
    Catching either needs a head anchored off this machine, which this version does not yet
    have. Point edits and mid-chain deletions are what it catches — and now it does.

## [0.9.2-alpha] — 2026-08-06

### Fixed

- **A provider pointed at your own server now keeps the model you configured.** No
  catalogue can know the model names of a server it has never seen. `openai-api` against a
  local Ollama is exactly that case: the built-in catalogue lists OpenAI's names, the
  server offers `qwen3.5:27b-int4`. Until now the unknown name fell back to an OpenAI
  model — **silently** — and Talos then talked to the local server under a name it does
  not have. A silent fallback to the *wrong* model is worse than a loud stop.
  - The exception hangs on a deliberate act by the operator: they set
    `TALOS_BASE_URL_<PROVIDER>`. It grants nothing and bypasses no kernel; it only says
    that here the catalogue is no longer the truth about which names exist.
  - Without that variable the catalogue still decides, and the fallback is still logged.
  - ⚠️ Found by running the path on a **fresh** machine. On a developer's box the model
    cache is warm from an earlier `models --refresh`, so the bug is invisible there.

## [0.9.1-alpha] — 2026-08-06

### Fixed

- **A fresh install ran into five tracebacks instead of a wall.** Installing Talos and
  typing `talos ask "hello"` produced a stack trace — five times in a row, for five
  different reasons. Four of them were unnecessary; the fifth is real.
  1. A messenger token was demanded by a command that never touches the messenger.
     ⚠️ `run()` loads the configuration a **second** time, which is exactly where the
     first attempt at this fix failed in August while nine unit tests stayed green.
  2. An empty allowlist raised. Without a messenger there is exactly one channel: the
     command line. Empty does not mean "open to everyone" there — it means "only whoever
     has a shell on this machine", and they can add themselves to the file anyway.
  3. The installer copied `.env.example` including its placeholder principal while
     announcing "empty allowlist". The line was simply false, and the placeholder
     defeated the affordance above, because a *set* allowlist is exhaustive.
  4. The configured default model is not in a fresh catalogue. The safety net meant to
     rescue the start was what prevented it.
- **Those failures now read as walls, not crashes.** The messages were always good — they
  name the reason and the way out. They just stood under thirty lines of stack trace,
  where nobody reads them. `TALOS_DEBUG=1` gives the trace back.

### Security

- **An empty allowlist admits the local CLI caller — and only that.** This is a loosening
  and is named as one. It applies solely when no messenger is being built (`ask`, `chat`),
  solely to the `cli` channel, and solely when the list is *empty*: a set list stays
  exhaustive and is never extended. A Telegram identity proves no access to the machine,
  so there the empty list stays closed. The caller is **really inserted** rather than
  merely waved through — otherwise the kernel would receive an empty set and refuse every
  action afterwards, which is worse than the wall it replaces. Four adversarial cases
  cover it, including a control case that must succeed.
- The model fallback is **logged, not silent**: anyone who set their model deliberately can
  find in the record why a different one ran.
- **The installer now enforces the checksum and the signature instead of displaying
  them.** It computed the sha256, printed it, and asked the reader to "compare with
  <url>" — it never fetched the published sum and never compared. The signature was not
  checked at all. So the one path that pipes a script from the internet into a shell had
  no enforcement, while `talos update` enforced both. That is backwards: the installer is
  the more exposed of the two, and it runs on a machine that has never seen this project
  before.
  - A missing sha256 tool is now a refusal, not a note that installs anyway.
  - The signature is verified with a vetted implementation in a **throwaway** environment
    — not with hand-written crypto in a shell script, and not with the tarball's own
    dependencies, which are precisely what is still unproven at that moment.
  - ⚠️ Order is the security here: both proofs run **before** anything is unpacked, and
    long before the suites from the archive are executed. A test asserts that order
    against the file rather than trusting the comment, and another asserts that the
    installer pins the *same* key as the updater — two paths drifting apart would mean
    one of them checks against something else.
  - Proven by refusal, not by success: an archive whose checksum *and* published sum were
    both replaced is rejected on the signature (exit 1, nothing unpacked); an archive
    altered without its sum is rejected on the checksum; a release with no signature at
    all is rejected outright.

## [0.9.0-alpha] — 2026-08-06

### Added

- **A message now steers the run already in progress.** Until now everything that arrived
  during a run was queued. That is right for a second, independent task and wrong for the
  sentence people actually send: "no, the other directory". The correction was written
  *because* the run is running, and putting it behind means letting the run finish wrongly
  first and then starting over.
  - It is injected **between two steps** of the agent loop, never into a running model
    call — that is a blocking subprocess and stays untouched. ⚠️ A single-step run
    therefore never sees a correction. That is the boundary, and it is stated rather than
    hidden.
  - `/queue <text>` is the opposite direction and deliberately bypasses steering: without
    it there would be no way to give a second task during a run at all. Bare `/queue`
    remains the status view, `/stop` still aborts.

### Security

- **The correction carries no additional rights.** It is a turn from the *same speaker*
  and enters the history framed as one. Every tool call after it passes the same kernel;
  a refused action is not permitted by a message sent afterwards. `redirect.py` imports
  neither `policy` nor `capability` nor `executor` nor `approval`, and that is asserted by
  parsing the module source — the same doctrine as `remedy`, `review` and `outcome`.
- **Two independent gates, because one would be the whole defence.** In the poll loop:
  if an approval is open, the next message is **its answer** — slipping it into a run as
  a course change while the operator believes they said "yes" to an action would be the
  most expensive mistake this path could make. In the mailbox: same principal **and** same
  conversation. Checking only the identity would let the same person talk into a run from
  another chat; checking only the conversation would let a **second allowed person** steer
  someone else's run.
- **Background runs stay unsteerable.** They run under the unattended ceiling — nobody is
  sitting in front of them, which is precisely the case that ceiling exists for. Runs
  resumed after an approval are not steerable yet either; that is a deliberate omission,
  not an oversight.
- Six new adversarial cases, one of them a control case that **must** get through — a
  suite in which nothing arrives proves nothing.

## [0.8.3-alpha] — 2026-08-06

### Fixed

- **The closing report no longer confuses an idle service with a stopped one.** Liveness
  was a five-minute window on the modification time of `data/eventlog.db`. On the Pi the
  systemd service was running and holding the old kernel in memory, but had not seen a
  message for hours — so the update signed off with "Nothing was started, nothing was
  scheduled", which is precisely the sentence that stops an operator from restarting.
  It now looks for a process whose `argv[0]` lies inside the installation (`/proc` on
  Linux, `ps` elsewhere), and the first line says what was measured rather than always
  claiming nothing runs.
  - ⚠️ **There are now three answers, not two.** `yes`, `no`, and **`I could not tell`** —
    the third used to collapse into `no`, and that was the whole defect. If nothing can be
    inspected, the report says so and names the consequence: the old kernel stays in
    memory until someone restarts it.
  - Not a heartbeat file: it would have to be written by the *running* version, which
    during an update is by definition the old one that does not know about it. A method
    that only works from the update after next does not answer the question being asked.
  - Not `systemctl`: a service is a process, a process is not always a service. Anyone who
    started Talos by hand appears in no unit.
  - ⚠️ Only `argv[0]`, never the whole command line. Measured against the real machine, a
    substring search reported a **demonstrably dead** tree as running — the pids were those
    of the checking command itself, which carried the path as an argument. A `grep`, an
    editor or a backup run would have done the same.

## [0.8.2-alpha] — 2026-08-06

### Fixed

- **The shipped guidance no longer names the private repository.** `CLAUDE.md` travels with
  the tree, so it is in the tarball and in the public repo — and it wrote out what that
  repository is called and what the operator's own installation is. No secret (the account
  name is in the author field of every public commit anyway), but nothing that belongs
  there either: addresses live in `git remote -v`, operational detail in the operator's own
  notes. ⚠️ 0.8.0 and 0.8.1 still carry the old text and are deliberately **not** replaced:
  publishing different bytes under a version that was already signed is exactly the swap
  the signature exists to prevent.
- **A guard now checks the published files instead of trusting a comment.** It reads the
  forbidden markers from `git remote -v` rather than spelling them out — a guard that
  writes down what it protects against leaks the same thing somewhere else, and this test
  ships too. It also follows a rename by itself instead of quietly rusting.

## [0.8.1-alpha] — 2026-08-06

### Fixed

- **A dead symlink in the workspace no longer stops an update.** Found the only way it
  could be: by running the real path. On the Pi, 0.7.0 → 0.8.0 downloaded, verified its
  signature, built a venv, passed 1485 tests and all 149 adversarial cases in the new
  tree — and then broke in the second-to-last step. `_carry_state` used `copytree`
  without `symlinks=True`, so it *followed* every link and copied the target; two links
  left behind by an earlier test run pointed nowhere, and a `FileNotFoundError` ended a
  release that had already proven itself. Copying the link instead of its target is also
  the truer image: a link pointing out of the tree would otherwise pull foreign content
  **in**.
- **A failure while carrying state no longer leaves a dead end.** The staging tree stayed
  behind, and `_require_absent` then refused every further attempt — citing an occupied
  path, without naming the original reason. The second run on the Pi needed a manual
  `rm -rf` first. Carrying state now sits inside the discard block: rebuilding a venv
  costs ten minutes, a dead end costs the update.
- **The copy step no longer contradicts itself.** It reported `data · workspace · SOUL.md
  — copied` and directly below `nothing of yours found next to the installation`. The
  `else` hung on the `for` instead of the `if`, and a `for/else` without `break` always
  runs — in the one step meant to prove the operator's own files came along.
- **A copy error now names the path.** `shutil.Error` carries a list of triples; unfiltered
  it lands in the message as a `repr` — six full paths on one line, the actual reason
  buried three times over.

## [0.8.0-alpha] — 2026-08-06

### Added

- **`/background <task>` — work beside the conversation.** Some questions take two minutes
  and the chat should not stand still for them. Security placement: a background run is a
  **scheduled run, not a subagent**. Not a subagent, because that one is born from model
  text and may only read — here a human typed the task. Like a schedule, because nobody is
  sitting in front of it, so anything needing approval is refused. Its context is empty and
  the result does not flow back into the history; it arrives as its own message, marked as
  a report. Capped at three at once, and refused outright if no ceiling is wired.
- **Context compression.** The conversation used to lose its oldest turns outright, so "and
  what was the second thing?" became unanswerable. Now the middle is summarised and both
  ends stay verbatim — the head carries the actual task, the tail whatever "and that too"
  refers to. The summary is labelled `Earlier (summarised)` rather than disguised as
  something said, and the transcript reaches the summariser framed as *data*: "summarise
  this" is otherwise the most convenient place for an injected line to become a permanent
  instruction. ⚠️ If summarising fails the oldest turns are dropped as before — a history
  that kept growing after a failure would turn a cost question into a leak.
- **`talos chat` — a session in the terminal.** `ask` answers once and leaves; anything
  multi-turn meant switching to Telegram on the very machine you were sitting at. Same
  channel name, same identity, same conductor, same kernel — the loop is line for line the
  one the service runs, only the messages come from `stdin`. In-session commands are the
  messenger's, through the same command centre.
- **`talos events` and `talos why <id>`.** The thin feeling with a guardian comes from not
  seeing what it did and why, not from missing verbs. `why` names the verdict, the rule
  that produced it and the targets it applied to — and states that those were *derived
  from the real arguments* — then shows the rest of the same run, because "refused, and
  then?" is the question that makes people stop reading logs. Both read-only. `undo` is
  deliberately not beside them: `/undo` is reachable from `talos chat` through the path the
  messenger uses, and a second one would be convenience against the doctrine.
- **A documented walkthrough** at `talos-agent.ch/docs/`, including a section on
  what is missing on purpose.
- **A lack is no longer answered like a refusal.** Every turn now carries what this
  machine is missing and what each gap costs — read from `doctor.py`, which has carried
  the remedy in its own text for weeks ("missing — `pip install ddgs`, no key needed").
  Only the operator ever saw it; the model never did. `SOUL.md` gains the matching rule:
  name the step or take it, and ask for the one thing that belongs to the operator rather
  than handing back the whole problem.
- **A mandatory self-review.** After a run, once per day at most, the agent reads its own
  log and reports what should change: the same wall hit repeatedly, approval prompts worn
  out by repetition, proposals the kernel refuses every time, and — the connection that
  makes the whole thing worth having — capabilities that are missing *and have actually
  cost runs*. Both halves existed already; nobody had put them side by side. Silence when
  there is nothing to say, and never into an open approval, where the next message is a
  "yes" that must not land on the wrong thing. `talos review` runs it on demand.
- **The agent learns from its own log.** A tool that failed the same way repeatedly is
  named before the next attempt — and the lesson expires the moment that same tool
  succeeds, so a fixed problem stops being carried as a warning.

### Security

- **The unattended ceiling now hangs on the terminal, not on the command name.** This is a
  loosening and is named as one: until now nobody could approve from the command line at
  all. Applying `ask`'s rule to an interactive session would mean `chat` could never write;
  dropping it would mean `talos chat < jobs.txt` in a cron job looks exactly like a human.
  So it is measured — **both** `stdin` and `stdout` must be a real tty. Checking only
  `stdin` would let `talos chat > out.log` pass as attended, and nobody reads a question
  written to a file. The channel name stays `cli` (one allowlist entry, not two) and the
  sandbox refusal applies unchanged. Five red-team cases cover it.

- **Neither `remedy` nor `review` can grant anything, and that is enforced against the
  source.** "Look for a way instead of refusing" is right for a lack and fatal for a
  verdict, where the same sentence means "propose a bypass". `remedy.py` therefore cannot
  see a verdict — it imports neither `policy` nor `capability` — and `review.py` has no
  function that could create a rule, however often an action was approved. Both are
  asserted by parsing the module source, not by reading the docstring. Five new red-team
  cases cover the block's framing and an injected instruction arriving through a doctor
  detail.

### Fixed

- **A run now states which tools failed, next to whatever the model said about it.** An
  installation answered "the vault note was created" while the log of that same run showed
  two failed writes and no successful one — it had even noticed the failures and
  confabulated a third, successful attempt. The kernel worked perfectly; the damage was in
  the *summary*, and no gate catches that. `outcome.note()` reads the run from the event
  log by `run_id` — not from the history the model has already seen — and appends the bare
  fact. It does not guess whether the answer is wrong.

- **A command-line run no longer steals the service's Telegram delivery.** `getUpdates` is
  exclusive per token: `talos ask` beside the running service produced `409 Conflict` for
  both and filled the log with channel errors. Telegram is now built only in service mode.

- **A long answer is split instead of thrown away.** Telegram refuses anything over its
  own limit; the reply was lost with a channel error rather than delivered in parts.

## [0.7.0-alpha] — 2026-08-05

### Security

- **Releases are signed, and an unsigned one is refused.** Archive and checksum came from
  the same base url: whoever controls the server, the CDN or the DNS replaces both, and
  the checksum then only proves the file arrived intact — not that it came from us. For an
  updater that is the worst kind of hole, because it ends in executing the code. Updates
  now verify an Ed25519 signature over the archive against `RELEASE_PUBLIC_KEY`, which
  ships **with the installed code** and therefore never sits on the server it guards.
  There is no fallback: a missing key, a missing signature or a missing `cryptography`
  refuses the update rather than proceeding on the checksum alone. ⚠️ Trust chains from
  the *running* tree — an update replaces that key too, so whoever lands one correctly
  signed update owns every one after it. That is true of any self-updater; it is stated
  here rather than left out.
- **A fetch connects to the address that was checked.** `guard_url` resolved the host and
  checked every address, then handed the URL *with its hostname* to the transport, which
  resolved again — a name could answer publicly for the check and internally for the
  connection. The checked addresses are now pinned per redirect hop. The name stays in SNI
  and the `Host` header, so the certificate is still verified against the name.
- **A refusal citing the model's own sandbox is not an answer.** The model declined tasks
  because its own process runs in plan mode with tools disabled — a restriction that is
  deliberate and says nothing about what it may request. It is corrected once and asked
  again. The end-to-end suite went from 7 failures to 2.

### Added

- The agent can run its own test suite: `PYTHONPATH` and the installation's own
  `.venv/bin` are set inside the sandbox (never inherited). Asked how it is doing, it can
  now answer with measured numbers instead of guessing.

## [0.6.0-alpha] — 2026-08-05

### Security

- **A key belongs to one provider.** There was a single `api_key` field, filled with
  `ANTHROPIC_API_KEY or OPENAI_API_KEY` and used for *every* provider — so selecting
  `openai-api` sent an Anthropic key to OpenAI as a bearer token. `credentials.py` now
  holds a route per provider, key **and** base url in one piece, resolved at call time
  because `/model` switches providers while the process runs. A provider without a key
  of its own can no longer be selected: the switch is refused and the previous provider
  stays active. `TALOS_API_BASE_URL` is retired — one address for every provider was the
  same mistake — and now stops the start rather than being silently ignored.
- **Mail fails closed without `TALOS_MAIL_AUTHSERV_ID`.** An `Authentication-Results`
  header nobody can attribute to a server proves nothing; previously the topmost one was
  believed, so a forged `dmarc=pass` next to a forged `From:` was enough to pass as an
  authenticated sender. Without the name your own receiving server stamps, every mail is
  now discarded, and `doctor` reports the missing name as a blocking finding.
- **The agent's own config file is inside the floor.** `talos.env` carries the bot token,
  the API keys *and* `TALOS_ALLOWED_PRINCIPALS`. It was an ordinary write target: the
  kernel stayed intact while its identity list came from a file the agent could rewrite.
  Reading is DENY, writing NEEDS_HUMAN. The kernel reads the path from the environment
  itself — a floor that asked the config module would protect the file only after it had
  been read.
- **Credential files of the usual tool chains are protected.** `~/.netrc`, `~/.aws`,
  `~/.gnupg`, `~/.docker/config.json`, `~/.kube`, `~/.git-credentials`,
  `~/.config/gcloud`, `~/.config/gh`, `~/.npmrc`, `~/.pypirc`. The sandbox mounts the
  root readable and masks exactly this list, so everything missing from it was readable
  inside the sandbox and reached the model through a tool result.
- **Credentials inside the notes vault are protected by folder name** — `credentials` or
  `secrets`, at any depth — instead of one hard-coded path that covered a single folder.
- **Scheduled runs cannot approve themselves.** `schedule.UnattendedCeiling` turns
  NEEDS_HUMAN into DENY when nobody is present. Autonomy without a gain in power.

### Added

- **`grab_frame`** — one still out of a video, so `see_image` has something to look at.
  Classified `Effect.READ` although it writes a file: the floor judges by effect, and as
  a WRITE a video under `~/.secrets/` came out as an approvable NEEDS_HUMAN while the
  same recording via `hear` is a hard DENY. The output path is derived by the kernel,
  never taken from the arguments.
- **`hear`** — local transcription via faster-whisper, no audio leaves the machine.
- **`web_search`** — Brave with a key, a keyless provider without one. No guessed
  substitute: without a provider the tool reports itself unavailable.
- **Photo inbox** — an incoming picture is fetched into `workspace/inbox/`, but the fetch
  asks the same allowlist the kernel uses and fails closed without it.
- **`talos ask`** — one turn from the command line. It is a channel like any other:
  `cli:<uid>` must be in the allowlist, the turn runs under the unattended ceiling, and
  it refuses inside the agent's own sandbox.
- **`talos doctor`** — what is missing. Changes nothing, no network without `--online`,
  prints no secret. Treats a config file owned by another user as the *better* state.
- **`talos config`** — `list`, `get`, `set`, `validate`. Writes only what `schema.py`
  classes as a setting; secrets and policy are refused, policy even with a confirmation.
  `config get` answers `[REDACTED]` for a secret whether it is set or not.
- **`talos models`** — the provider catalogue, `--refresh` asks the providers themselves.
- **`session_search`** — a durable FTS5 archive of past turns.
- **Mail as a second way in** — IMAP fetch, `Trust.ASK`, sender proven or discarded.
- **Sectioned setup wizard** — `talos setup <section>` re-runs one part without proving
  the identity again.

### Removed

- **Image generation.** Vision stays, generation goes: an agent that can produce
  photographs is a different risk conversation than one that can only look at them.

## [0.5.0-alpha] — 2026-08-03

First public release: policy kernel, capability tokens, sandboxed shell, Telegram
channel, approval flow with standing rules, plans, delegated runs, browser rendering,
event log, and the updater.
