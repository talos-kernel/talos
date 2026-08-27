# CLAUDE.md — Talos

Guidance for Claude Code (and any other coding agent) working in this repository.

## What this is

An autonomous agent that takes instructions over a chat channel, reasons with a language
model, and executes tools — but only after a deterministic security kernel has ruled on
the action. Full architecture in `README.md`. The agent's name and character live in
`SOUL.md`; durable operating discipline lives in `AGENTS.md`, and stable operator
preferences live in `USER.md`. All three are operator-owned prompt state and reload live.

| | |
|---|---|
| Gate path | `policy.py`, **589 lines** — has to stay readable in one sitting |
| Tools | **21**, every one gated |
| Suites | **1768** tests · **173** adversarial · 44 end-to-end |
| Home | <https://talos-agent.ch> · docs at `/docs/` |
| Repository | `talos-kernel/talos` is the public source tree |

⚠️ **`public` is blocked for pushing.** Its push url is a deliberate dead end — the
published state is only ever produced by `scripts/sync-public.sh` into a separate clone.
A direct push from here would carry 108 commits and a real author address across.

## The rule everything rests on

**The model proposes, it does not decide.** The reasoner emits `TOOL_CALL` text, the kernel
judges, the executor performs. Weakening that order destroys the product, no matter how
green the tests are.

In practice:

- **Never put security logic in `tools.py`.** The runners are deliberately dumb. Gate
  (policy) and execution (runner) stay separate.
- **Never trust a field the model could omit.** Targets are derived from the real arguments
  via `TARGET_EXTRACTORS`; a tool without an extractor is `DENY` by construction.
- **Never give the reasoner its own tools.** `DISALLOWED_TOOLS_ARGV` and
  `CLAUDE_ISOLATION_ARGV` in `reasoner.py` are security boundaries, not tuning.
- **Never let the model write the approval text.** It comes from the kernel, so the human
  sees the facts rather than the model's description of itself.
- **Never let a plan carry permission.** An announced sequence (`plan.py`) may set order,
  an abort condition and a per-step check — nothing else. Every step still passes
  `decide()` on its own, and there is deliberately no "approve the plan" path: that would
  be consent to actions nobody had seen yet. A plan may only make a run end *earlier* or
  withhold a confirmation; it may never grant one.
- **Never let a plan check touch the world.** `check_met` reads the receipt of the step
  that just ran and nothing else. A condition that could open files would be a read oracle
  around the kernel. Unknown check vocabulary is dropped, never treated as met — the other
  direction would make invented words a way to fake an acceptance.
- **Never let a delegated run write.** `subagent.ReadOnlyCeiling` turns everything except
  reading into `DENY`, including anything that would need approval — a question reaching
  the operator out of the context it came from is how reflexive clicking starts. A
  subagent is born from model text; it must be able to do *less* than its caller.
- **Never let the browser operate a page.** `browse` renders and reads. Clicking, typing
  and form submission have no derivable target, so they cannot be gated — and a tool
  without a target is `DENY` by construction. Rendering also stays inside the resolver
  cage (`browser.resolver_rules`), so a redirect cannot leave the host `guard_url` checked.
- **Never add a second source of permission.** The autonomy dial and the channel ceiling
  can only tighten. Anything that grants rights next to the kernel reintroduces the exact
  problem capability tokens were built to remove.
- **Never let a channel receive.** Every way in fetches: Telegram long-polls, `mail.py`
  pulls over IMAP. A webhook needs a port the world can reach, which turns an
  outbound-only process into a reachable one — that is why inbound WhatsApp does not
  exist and `WhatsAppChannel.poll()` returns `[]` on purpose rather than "not built yet".
- **Never fetch on a stranger's say-so.** The channel parses updates *before* the kernel
  has ruled on identity. An incoming photo is fetched into `workspace/inbox/` so
  `see_image` has a target — but the fetch asks the same allowlist the kernel uses, and
  fails closed without it. Otherwise anyone who finds the bot can write to the disk.
- **Never let a second door be softer than the first.** `grab_frame` is `Effect.READ`
  although it writes a file, and that is deliberate: the floor judges by effect, not per
  target (`policy.decide`, step 4), so as a `WRITE` a video under `~/.secrets/` came out
  as an approvable `NEEDS_HUMAN` while the same recording via `hear` is a hard `DENY`.
  Frame capture would have been the softer way to the same content. What decides the
  effect is the target that can be *chosen* — the source, which is read. The picture's
  path is derived by the kernel (`policy.frame_output_path`), never taken from the
  arguments, so the model cannot pick where bytes from a foreign file land; the runner
  calls that same function rather than rebuilding the rule, because a rebuilt rule drifts
  and the kernel would then be judging a file that never appears.
- **A background run is a scheduled run, not a subagent.** `/background` is typed by a
  human, so its origin is an ordinary message and `ReadOnlyCeiling` would be the wrong
  tool. But nobody watches it, so `UnattendedCeiling` applies — the *same instance* the
  schedule ticker uses, because two ceilings would be two truths. Its context is empty
  (`past_override=()`): two runs sharing a history write into each other. ⚠️ Without a
  wired ceiling the task is **refused**, never run uncapped — `conductor._start_background`
  checks that first, and a red-team case holds it. Capped at `MAX_CONCURRENT`, and the
  refusal is immediate rather than a silent queue.
- **Compression may fail; the bound may not.** `memory._trim` summarises the middle when a
  summariser is wired and discards afterwards regardless. That order is deliberate: a
  history that keeps growing after a failed summary turns a cost question into a leak. The
  summary is labelled `Earlier (summarised)`, never disguised as a verbatim turn, and the
  transcript reaches the summariser framed as data — "summarise this" is otherwise the most
  convenient place for an injected line to become a *permanent* instruction.
- **The answer is not the receipt.** After a run, `outcome.note()` appends the tools that
  failed and did not succeed afterwards — read from the **event log via `run_id`**, never
  from the agent history the model has already seen. Measured cause: an installation
  answered "the note was created" while the log of that same run showed two failed writes
  and no successful one. The kernel was flawless; the *summary* was not, and no gate
  catches that. It states the fact and does not guess whether the answer is wrong — text
  interpretation would be unreliable in both directions. Fail-open: a broken log costs the
  note, never the answer.
- **The ceiling hangs on the terminal, not on the command name.** `talos chat` is a
  loosening: until it existed, nobody could approve from the command line at all. The
  condition is *measured* — `chatcli.attended()` requires **both** `stdin` and `stdout` to
  be a real tty. Checking only `stdin` would let `talos chat > out.log` pass as attended,
  and nobody reads a question written to a file; a pipe, a redirect or a cron run keeps
  the ceiling and `NEEDS_HUMAN` stays `DENY`. The channel name stays `cli`, so there is
  one entry in the allowlist rather than two, and the sandbox refusal from `askcli`
  applies unchanged. Five red-team cases hold this.
- **Telegram is only built in service mode.** `getUpdates` is exclusive per token, so a
  `talos ask` beside the running service stole its delivery and both got `409 Conflict`.
  A command-line run answers where it was started; it does not need the messenger.
- **Never let the command line be a shortcut into the agent.** `talos ask` is a channel
  (`askcli.CliChannel`) with the same protocol and no special right. `cli:<uid>` must be
  in the allowlist like any Telegram number — a shell beside the agent is not an argument
  for granting it something. The turn always runs under `schedule.UnattendedCeiling`,
  even when a human types it: a one-liner waits for nothing, so `NEEDS_HUMAN` becomes
  `DENY` rather than an approval the caller grants themselves. And it refuses when
  `sandbox.MARKER` is in the environment — otherwise the agent starts `talos ask` from
  its own shell and gives itself orders, with no channel and no reader.
- **Never let a command line grant power.** `config set` writes only what `schema.py`
  classes as `SETTING`. `SECRET` and `POLICY` are refused — the latter *even with a
  confirmation*, because a confirmation is what gets clicked away. The criterion is not
  the name but the effect: can a change admit a commander, loosen a kernel filter,
  redirect protected data, or replace credentials? Then it is policy. `config get`
  answers `[REDACTED]` for a secret **whether it is set or not**; anything else (stars by
  length, a prefix, a `last4`) turns the command into an oracle for which doors exist.
  And a value may not contain a newline: it would append a second line that can set any
  other key, including the allowlist.
- **The hardest boundary is not the floor, it is the filesystem.** The floor stops
  `write_file`; the sandbox stops the shell. Both are code in the same process. While the
  agent and its config file belong to the same writable uid, a bug in either is enough.
  The real separation is ownership: the config belongs to another user (root), the
  directory too — otherwise write access to the directory lets the agent delete and
  recreate the file — and the agent may only read. `doctor` treats that as the *better*
  state, not a finding: mode 640 owned by someone else is stricter than 600 owned by
  yourself, and a doctor that reports the stricter setup as a defect teaches people to
  undo it. ⚠️ `updater._carry_state` uses `shutil.copy2`, which carries the mode but not
  the owner — only root can do that. If that ownership was the boundary, the copy after
  an update belongs to the agent again. The updater says so instead of pretending.
- **Never leave the agent's own config file outside the floor.** `talos.env` carries the
  bot token, the API keys *and* `TALOS_ALLOWED_PRINCIPALS`. Until 2026-08-05 it was an
  ordinary `write_file` target with ALLOW — the kernel stayed intact while its identity
  list came from a file the agent could rewrite. It is Tier A now: reading DENY, writing
  NEEDS_HUMAN. `policy._config_files` reads the path from the environment itself; a floor
  that asked `config.py` would protect the file only after it had been read.
- **Never resolve a name twice.** `guard_url` returns the addresses it checked in
  `SafeUrl.addresses`; `fetch_page` hands them to the transport as `pin`, per hop, because
  each hop is checked separately and has its own. Without that, the library resolved again
  between check and connect and a name could answer publicly the first time and internally
  the second. The name stays where it belongs — SNI *and* the `Host` header — so the
  certificate is still checked against the name, not the address. ⚠️ `urllib3` derives
  `Host` from the *pool's* host, which is the IP once pinned: without setting it by hand
  every CDN-fronted site answers 403. That was measured, not assumed — the same address
  gave 403 pinned and 200 unpinned with identical SNI, while the comment above the code
  already claimed the header was preserved. A transport that cannot pin must refuse, never
  fetch unpinned: a fetch that silently drops the pin looks exactly like a safe one.
- **Never let one credential serve every provider.** `credentials.py` holds a route per
  provider — key *and* base url in one piece — and `ApiReasoner` resolves it in `_route()`
  at call time, not in `__init__`. Both halves matter: until 2026-08-05 `config.api_key`
  was `ANTHROPIC_API_KEY or OPENAI_API_KEY` for *every* provider, so `openai-api` sent an
  Anthropic key to OpenAI as a bearer token, and `config.api_base_url` was the same
  mistake one line down — an OpenAI-shaped request aimed at whatever address was last
  configured. Call-time resolution is deliberate even though `ModelRouter` rebuilds on
  every switch: correctness must not depend on a property somebody may optimise away.
  A missing key fails the *switch* (`_build_validated` runs before the swap), so the
  previous provider stays active instead of a turn dying mid-conversation — and the
  message names the variable, never its value. `_scrub` masks **every** stored key, not
  just the active one: a foreign server echoes back the header it received.
- **Never build an identity out of an unproven sender.** A `From:` header is chosen by
  the sender and IMAP verifies nothing, so an allowlist keyed on it is bypassed by one
  line of text. `mail.verify_sender` trusts only the `Authentication-Results` our own
  receiving server stamped, takes the **topmost** one (ours is prepended; an injected one
  sorts below), and fails **closed** when there is none. Mail stays `Trust.ASK`; the
  property has no setter.
- **Never answer a lack the way you answer a verdict.** Since the self-review, every turn
  carries what the machine is missing (`remedy.py`, fed from `doctor.py`), and `SOUL.md`
  tells the agent not to stop at "I can't". That is right for a *lack* — a library that
  was never installed, a key that is not set — where the prerequisite is nameable and
  usually one command away. Applied to a *verdict* the same sentence reads "propose a way
  around the gate", which would end the product. The line holds because `remedy.py`
  physically cannot see a verdict: it imports neither `policy` nor `capability`, and a
  test asserts that against the source rather than the intent. The step it names is not a
  permission either — `pip install ddgs` is `run_shell` and passes the same chain.
- **Never let the review apply itself.** `review.py` counts repeated failures, worn-out
  approval prompts, repeatedly refused proposals, and gaps that actually cost runs; it
  proposes and it delivers, it never writes a rule. A standing rule created from "you
  approved this three times" would be a second source of permission built out of the
  agent's own history — the exact thing capability tokens exist to prevent. The module
  has no function that could create one, and `redteam.py` checks that too. It also counts
  how often it has reported the same finding before: a review that mails the same list
  every week without noticing is a ritual, and rituals get wiped unread.

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

python -m pytest tests/ -q   # 1768 tests, ~8s
python redteam.py            # 173 adversarial cases — mandatory for any kernel change
python e2e.py                # 44 cases against a real model (costs tokens and time)
python -m talos --once       # single cycle, for diagnosis
python -m talos              # run

python -m talos doctor       # what is missing — changes nothing, no network without --online
python -m talos config list  # keys, their kind, whether they are set
python -m talos models       # the catalogue; --refresh asks the providers
python -m talos status       # what the event log says it did last
python -m talos health       # runs, errors, schedules, last anchor — no network, exit 1 on a broken chain
python -m talos report       # the event log as a record a third party can read
python -m talos review       # what this installation should change — writes nothing
python -m talos ask "…"      # one turn, no chat — cli:<uid> must be in the allowlist
python -m talos chat         # a session here; the ceiling hangs on the tty, not the command
python -m talos events       # what happened — read-only, filterable
python -m talos why <id>     # why that was allowed or refused, and what came of it
python -m talos verify       # prove the event log was not edited after the fact (exit 1 if it was)
python -m talos anchor       # pin the chain head — exit 1 if the log shrank (--send mails the digest)
```

## Traps that have already cost time

1. **Two pollers give `409 Conflict`.** Telegram's `getUpdates` is exclusive per token.
   Stop any running instance before starting one by hand.
2. **`redteam.py` and `e2e.py` assert on user-facing text.** Change a string and they fail
   even though behaviour is correct. Update them in the same commit.
3. **Tests must not hardcode home paths.** Use `Path.home()`; a literal `/home/<name>`
   passes on one machine and silently tests nothing on another — including at the secret
   floor, where a false pass is worse than a failure.
4. **`data/` is gitignored and stays that way.** The event log contains recorded commands.
5. **Stop the display's heartbeat thread before writing the final state**, or a tick
   overwrites the frozen trail with the live view.
6. **The status display must not appear for tool-free answers.** A plain reply gets no
   header — that is a deliberate decision, not a style preference.
7. **A green suite can miss a broken service.** `run()` once read a name that does not
   exist; 1215 tests stayed green while the process crash-looped. `tests/test_media.py`
   now checks the composition root with `symtable` — but that sees names, **not
   attributes**. A second check reads the runner map out of `run()` with `ast` and holds
   it against the manifest, so a tool that is offered but not wired fails here instead of
   at the model's first call. Neither replaces starting it: after wiring anything new,
   run `python -m talos --once`.
8. **An empty event log is not "never chosen".** The model selection lives there; a log
   that did not survive an update makes a deliberately configured install answer with the
   shipped default. The fallback stays, but it leaves a trace — and a real deployment
   names its model in `talos.env` so the fallback is *its* model, not this repo's.
9. **Two repositories, and the remotes say which is which.** In this working tree
   `private` carries the full history and the real author address; `public` is
   `talos-kernel/talos`, an own clean history from zero. Run `git remote -v` for the
   addresses — ⚠️ they are deliberately **not** written out here, because this file is
   published with the tree, and the private one has no business being in it. The
   deployment target is neither: it is a running instance fed by `rsync`, and a third
   remote for it no longer exists.
   ⚠️ Moved into the organisation on 2026-08-06 by pushing fresh, **not** by transfer:
   a transfer leaves a permanent redirect from the personal account, and with zero stars
   it would have bought nothing. The old repo was deleted, so no redirect exists.
   ⚠️ **`public` is blocked
   for pushing** — its push url is a deliberate dead end. The public state is only ever
   produced by `scripts/sync-public.sh` into a separate clone; a direct push from here
   would carry the whole private history across.
10. **An operator installation is not this repo.** The instance on the operator's machine has
   its own `SOUL.md` (its first heading is the agent's *name*), its own `CLAUDE.md`,
   its own env file and its own `data/`. Sync the **package** (`talos/` → `talos/`), never
   the repository root, or the deployment gets renamed and loses its event log — which is
   also where the chosen model lives.

## Conventions

- Comments and docstrings explain **why**, especially where a rule looks counterintuitive.
  Those are the ones that get argued away six months later.
- Small modules. The gate path (`policy.py`, 589 lines) must stay readable in one sitting.
- Glyphs come from `talos/ux.py` only, one meaning each, **never inside an answer's prose**.
- Telegram edit interval stays ≥ 1.2 s; the API tolerates roughly one edit per second
  per chat.
- User-facing conversation follows the user's language (`SOUL.md` mandates this). The
  machine console — status display, `/help`, `/policy`, `/debug`, kernel reasons — stays
  English.
- Approval tokens are additive across languages (`yes` **and** the operator's own word).
  Never replace, only extend.

## Changing the security kernel

Applies to `policy.py`, `capability.py`, `command_floor.py`, `approval.py`, `standing.py`,
`autonomy.py`, `trust.py`, `verifier.py`, `executor.py`, and the three ceilings that sit
above the kernel (`schedule.py`, `plan.py`, `subagent.py`).

1. Branch; never commit straight to `main`.
2. `pytest`, then `redteam.py`, then `e2e.py` — all green.
3. **Any loosening must add a red-team case** proving the boundary beside it still holds.
4. Say plainly in the commit message what the change makes possible that was not possible
   before. A security change whose blast radius is not written down is not reviewable.

**The shell is sandboxed.** `run_shell` runs under bubblewrap on Linux and
`sandbox-exec` on macOS: root read-only, the workspace the only writable place, network
off, environment reduced to an allowlist. Where no isolation is available it **refuses**
rather than running unprotected; the operator can override that only on purpose
(`TALOS_SANDBOX_ALLOW_UNCONFINED=1`).

This closes the gap this file carried for months: the path floor only ever saw literal
tokens, so `P=/etc; cat $P/passwd` walked past it, as did `eval` and anything rebuilt
from base64. The sandbox does not guess what a command will do — it bounds what it can.
Both stay: the floor catches the obvious early and gives the approval text its path
labels, the sandbox holds the rest.

`tests/test_sandbox.py` runs those attacks for real rather than against doubles, and
skips instead of claiming green where a platform has no implementation.
