<p align="center">
  <img src="assets/talos-icon-256.png" alt="Talos icon" width="132">
</p>

<h1 align="center">TALOS</h1>

<p align="center">
  <em>An autonomous agent you can hand a shell to,<br>because it can prove what it will not do.</em>
</p>

<p align="center">
  <a href="https://talos-agent.ch"><b>talos-agent.ch</b></a> ·
  <a href="https://talos-agent.ch/docs/">Field manual</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <!-- ⚠️ Bewusst „tests", nicht „passing": die Zahl kommt aus dem Einsammeln (2343).
       Plattformabhaengige Sandbox- und Repository-Pruefungen koennen uebersprungen werden;
       `test_site_claims` prueft deshalb die gesammelte Zahl statt ein Umgebungsresultat. -->
  <img src="https://img.shields.io/badge/tests-2343-2e7d32.svg" alt="Tests">
  <img src="https://img.shields.io/badge/red%20team-208%2F208-2e7d32.svg" alt="Red team">
  <img src="https://img.shields.io/badge/gate%20path-895%20lines-8a4318.svg" alt="Gate path">
  <img src="https://img.shields.io/badge/tools-28%20gated-8a4318.svg" alt="Tools">
  <img src="https://img.shields.io/badge/default%20identities-0-c62828.svg" alt="Default identities">
  <img src="https://img.shields.io/badge/python-3.11%2B-1565c0.svg" alt="Python">
  <img src="https://img.shields.io/badge/licence-MIT-616161.svg" alt="MIT">
</p>

---

Talos runs on your own machine, takes instructions over a chat channel, thinks through a
language model, and executes tools — but only after a deterministic security kernel has
ruled on the action. **The model proposes. It never decides.**

```
   message ──▶ event log ──▶ reason ──▶ ╔══════════╗ ──▶ capability ──▶ execute
                                        ║  KERNEL  ║        token          │
                                        ╚══════════╝                       ▼
                                             ▲                     verify ──▶ receipt
                              the only place effects are authorised
```

| | |
|---|---|
| **Authority** | a token bound to exact arguments — valid **once**, for **30 seconds** |
| **A tool without a target extractor** | `DENY` by construction, not by a rule someone wrote |
| **Ships with** | **zero** identities that may command it |
| **The shell** | sandboxed, or it refuses to run at all |

```bash
curl -fsSL https://talos-agent.ch/install.sh | less   # read it first
curl -fsSL https://talos-agent.ch/install.sh | sh     # then run it
```

The installer verifies the signature and the checksum, runs the full suite — and then
**stops**. Nothing starts listening until you say so.

---

<details>
<summary><b>Contents</b> — twenty-two sections, in the order they matter</summary>

**Start here**
[Why this exists](#why-this-exists) ·
[What it does not do](#what-it-does-not-do) ·
[Install](#install)

**Using it**
[A session in the terminal](#a-session-in-the-terminal) ·
[Work on the side](#work-on-the-side) ·
[What it remembers](#what-it-remembers) ·
[Seeing what it did](#seeing-what-it-did) ·
[Commands](#commands)

**How it holds**
[How the kernel decides](#how-the-kernel-decides) ·
[The autonomy dial](#the-autonomy-dial) ·
[Timed runs](#timed-runs) ·
[Announced plans](#announced-plans) ·
[The browser that only reads](#the-browser-that-only-reads) ·
[Delegating](#delegating) ·
[WhatsApp through your own broker](#whatsapp-through-your-own-broker) ·
[The MCP registry](#the-mcp-registry) ·
[Identity](#identity)

**Evidence**
[What a run looks like](#what-a-run-looks-like) ·
[Audit trail](#audit-trail) ·
[Tools](#tools) ·
[Architecture](#architecture) ·
[Roadmap](#roadmap)

</details>

## Why this exists

Every capable agent eventually asks for shell access. At that moment you are trusting a
language model with your machine, and the usual answers are unsatisfying: either it asks
you to confirm everything (and you stop reading the prompts by day three), or it runs free
behind a regex blocklist and you hope.

Talos takes a third position. **Authority is a token, not a list.** Every effect is
authorised individually, bound to its exact arguments and targets, valid once, for thirty
seconds. Forgetting to call the gate does not produce an unchecked effect — it produces no
effect at all, because the raw runners are unreachable without a token.

That design is testable, and it is tested: 208 adversarial scenarios run on every change and
try to get an effect past the kernel. They are in [`redteam.py`](redteam.py). Read them
before you trust anything written above.

## What it does not do

Stated plainly, because a security claim without its limits is marketing:

- **`run_shell` needs a sandbox the platform can actually provide.** It runs under
  bubblewrap on Linux and `sandbox-exec` on macOS. Where neither is available it *refuses*
  rather than running unprotected, so on such a host the shell tool is simply unavailable
  until the operator overrides it on purpose (`TALOS_SANDBOX_ALLOW_UNCONFINED=1`).
- **It is not a multi-tenant security boundary.** One operator, one machine. Anyone who can
  run code in the process can reach the token mint.
- **There is no gateway, no setup portal and no `config.yaml`.** Each was considered and
  left out, for the same reason. A gateway terminates foreign identities and then *claims*
  `channel:id` to the kernel instead of proving it. Identity checking would then hang on a
  component outside the kernel. A web portal is a network service with its own
  authentication, so it is a second source of permission beside the allowlist. And a
  second config file would be two truths about who may command it, when the path floor
  protects exactly one. What weakens it is not the number of commands. It is a second
  way in.
- **It does not defend against a malicious model.** It defends against a *mistaken* one,
  and against prompt injection arriving through tool output. Those are different threats.
- **Search needs no account.** Without `TALOS_BRAVE_API_KEY` it used to refuse; now it
  answers over DuckDuckGo (the `ddgs` package, imported at call time). A key decides
  *which* provider answers, not *whether* one does.
- **It has three ways in, and all of them fetch** (Telegram long-polling, mail over
  IMAP, the WhatsApp broker queue over SSH). That is the rule, not an accident: an
  inbound webhook would need a port the world can reach, which turns "outbound only"
  into "publicly reachable". Mail sits at `Trust.ASK` — an address proves no account,
  so it may ask and receive answers but never approve anything. WhatsApp over the
  Cloud API stays delivery-only for the same reason; the broker channel below is the
  one that fetches.
- **It hears locally or not at all.** `hear` transcribes a recording with faster-whisper
  on the machine it runs on — what was said is often the most private thing in a day, and
  sending it to somebody else's model to be understood is the one place where "runs on your
  machine" would be traded for convenience. It is an ordinary `READ` with the file as its
  target, so a recording under `~/.secrets/` is refused without the module knowing anything
  about secrets. The model loads on first use, not at boot.
- **It can see, on a real file.** A photo you send is fetched into `workspace/inbox/` and
  the note carries the path, so `see_image` has something to point at — and the kernel has
  a target to judge. The fetch asks the allowlist first: the channel parses updates before
  the kernel has ruled on identity, so an unconditional fetch would let anyone who finds
  the bot write files to your disk. `speak` writes a WAV offline (piper).
- **It can take one still out of a video.** `grab_frame` runs ffmpeg once and puts a
  single picture in the inbox; `see_image` looks at it afterwards. Both the video and the
  picture are targets the kernel judges, so a video under `~/.secrets/` is refused exactly
  like a recording — without that, calling a file a video would have been the way around
  the secret floor. Where the picture lands is derived by the kernel, not chosen by the
  model, and it is deliberately one frame rather than a series: fanning ffmpeg out over a
  whole film and encoding every frame is how a small machine ends up unreachable.
- **It does not generate pictures or video, on purpose.** Reading one is a `READ` with a
  target; making one is a paid call to somebody else's model, and it was removed rather
  than kept around waiting for credit. Seeing stays.

## Install

Requires Python 3.11+ and a working [Claude Code CLI](https://claude.com/product/claude-code)
(the reasoner runs it headless via OAuth — no API key, no per-token billing).

```bash
git clone https://github.com/talos-kernel/talos.git
cd talos
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock

python -m talos setup                    # asks three things, writes a file, stops
python -m talos doctor                   # what is still missing
python -m pytest tests/ -q               # 2343 tests
python redteam.py                        # 208 adversarial cases
python -m talos                          # run it
```

`setup` proves what it can rather than trusting what you type: the token goes to `getMe`,
and your identity comes from a **real message you send your own bot** — not from a number
you copy. A wrong token announces itself on the first poll; a wrong identity does not, and
a stranger's identity never does. It writes the file and stops; starting is yours.

Later, a single part can be redone without repeating the rest:

```bash
python -m talos setup model              # switch what it thinks with
python -m talos setup mail               # add the second way in (IMAP)
python -m talos config list              # every key, its kind, whether it is set
python -m talos config set TALOS_MODEL claude-fable-5
python -m talos config set TALOS_MODEL_OVERRIDES '{"local-model": {"context_window": 128000}}'
                                         # a window or a price the catalogue does not know —
                                         # never a provider, url or key; shown as your word
python -m talos models --refresh         # ask each provider what it offers now
python -m talos status                   # what it did last
python -m talos chat                     # a session here; approvals at a terminal
python -m talos ask "how many …?"        # one turn from here, answer on stdout
python -m talos events --tool run_shell  # what happened — read-only, filterable
python -m talos why 4831                 # why that was allowed or refused
python -m talos review                   # what this installation should change
python -m talos report --out audit.txt   # what was done and what was refused
```

A full walkthrough — install, identity, the session, every command, and the ones that are
missing on purpose — is at **[talos-agent.ch/docs](https://talos-agent.ch/docs/)**.

`ask` is not a second way in. It is a **channel like any other**, with the same protocol
and no special right. Two consequences follow, and both are the point of it. While the
allowlist is empty the local caller is admitted as `cli:<uid>` automatically, so the first
run works. The moment the list names one identity it becomes exhaustive, and the uid must
appear as `cli:<uid>` exactly like a Telegram number. A shell next to the agent is not an
argument for granting it anything. And the turn always runs under
the **unattended ceiling**: a one-liner waits for nothing, so `NEEDS_HUMAN` becomes `DENY`
and says so. Approve in the chat, where somebody is actually looking.

It also refuses to run inside the agent's own sandbox. Otherwise the agent could start
`talos ask` from its shell and give itself orders — no channel, no foreign identity,
nobody reading along.

## A session in the terminal

`ask` answers once and leaves. For anything that takes more than one turn there is
`talos chat` — the same channel name, the same identity, the same conductor, the same
kernel. The loop is line for line the one the service runs for Telegram; only the messages
come from `stdin` instead of `getUpdates`.

```
$ talos chat
Talos 0.9.2-alpha  ·  anthropic/claude-opus-4  ·  autonomy 3
speaking as cli:1000  ·  approvals possible — you are at a terminal
/help for commands, `exit` to leave

› what changed in the log today?
```

The second line is the one to read: it says, before you ask for anything, whether an
approval is even possible here — so a refusal later is explained in advance rather than
guessed at.

**The unattended ceiling hangs on the terminal, not on the command.** This was the
contested decision. Applying `ask`'s rule to an interactive session would mean `chat`
could never write anything; dropping it would mean `talos chat < jobs.txt` in a cron job
looks exactly like a human. So it is measured rather than claimed: **both `stdin` and
`stdout` must be a real tty**. A pipe, a redirect, a cron run has none, and there
`NEEDS_HUMAN` becomes `DENY` as before. Checking only `stdin` would let
`talos chat > out.log` pass as attended — nobody reads a question written to a file.

The in-session commands are the same ones the messenger has, through the same command
centre. A second vocabulary that only worked here is exactly the duplication this project
has paid for elsewhere.

## Work on the side

Some questions take two minutes, and the conversation should not stand still for them.

```
› /background go through /var/log and summarise today's errors

Background #1 started: go through /var/log and summarise today's…
  id bg_a1b2c3d4e5f6 · runs unattended, so anything needing approval is refused
```

**A background task is a scheduled run, not a helper carrying your rights.** That is the
whole security decision, and it avoids inventing a new concept. It is *not* a subagent:
that one is born from model text and may therefore only read. Here a human typed the task,
so its origin is the same as any other message. But nobody is sitting in front of it — so
the ceiling from the timed run applies, and anything that would need approval is refused.
Putting a question into a chat where a different conversation is running is the most
reliable way to land a "yes" on the wrong thing.

Its context is **empty**: the task and nothing else. Two runs sharing one history write
into each other, and afterwards nobody can say which of them said what. The result does not
flow back into the history either — it arrives as its own message, marked as a report.

If no ceiling is wired, the task is **refused** rather than run uncapped. A forgotten
parameter may only ever allow less.

A running background task can be **steered**: `delegate_steer` queues a course correction,
and the task reads it at its next step boundary — the same seam where a typed correction
enters a foreground run, only from its own mailbox. The instruction arrives as a framed
turn ("no additional rights"); every tool call it provokes passes the same kernel under
the same ceiling. Only the person and conversation that started the task may steer it,
a delegated run may not steer at all, and anything without a step boundary in this
process — a synchronous `delegate`, a worker job, a timed run — answers with a refusal
rather than a pretend "ok".

And everything stoppable stops at once: `/stopall` (also `/estop`) aborts the running
thought, drains the queue, detaches every background task at its next step and discards
its report, and **discards** every pending approval — it never approves one. Timed runs
stay; a stop that deleted timers would create the next incident while ending this one.
The reply is an honest balance per category, and a second `/stopall` says so.

## What it remembers

The conversation is kept per channel, in memory only, bounded by turns and by characters.
When the bound is reached the **middle is summarised** and both ends stay verbatim — the
head usually carries the actual task, the tail whatever "and that too" refers to.

```
You:                  set up the backup for the NAS
Agent:                done — see /etc/backup.d
Earlier (summarised): discussed retention, settled on 30 days; the NFS
                      mount needed the hard option
You:                  and add the offsite copy
```

The summary is labelled as one and never disguised as a verbatim turn — a summary that
looks like something actually said is a claim about words nobody used, and the model could
quote it back as if it were. It goes into the prompt as history, never into the standing
instructions, and the transcript reaches the summariser explicitly framed as *data*:
otherwise "summarise this" is the most convenient place for an injected line to become an
instruction, and a permanent one, because the summary stays.

⚠️ **The bound is not a comfort feature.** If summarising fails, the oldest turns are
dropped as before. A history that kept growing after a failed summary would turn a cost
question into a leak — what was said weeks ago would go out again.

## Seeing what it did

The thin feeling with an agent like this does not come from missing verbs. It comes from
not being able to see what it did and why. Both of these are **read-only**:

```bash
python -m talos events --tool run_shell   # what happened, filterable
python -m talos why 4831                  # why that was allowed or refused
```

`why` names the verdict, the rule that produced it, and the targets it applied to — and
states that those targets were *derived from the real arguments*, never taken from what
the model wrote. Then it shows the rest of the same run, because "refused, and then?" is
the question that makes people stop reading logs.

There is deliberately no `talos undo` beside them. `/undo` exists, and since `talos chat`
it is reachable from the command line through the same path the messenger uses. A second
one would be convenience against the design.

**`config set` refuses two whole classes of key**, and that is the point of it. Secrets
never go on a command line — that lands them in the shell history and in `ps` for every
user of the machine. And `TALOS_ALLOWED_PRINCIPALS`, `TALOS_SECRETS_ENV`,
`TALOS_MAIL_AUTHSERV_ID`, every `TALOS_BASE_URL_<PROVIDER>` and
`TALOS_WEB_ALLOWED_ADDRESSES` are not settings but *policy*:
whoever writes them does not have to talk the kernel into anything, they reconfigure it.
Not settable, not even with a confirmation — a confirmation is exactly what gets clicked
away. `config get` answers `[REDACTED]` for a secret **whether it is set or not**, so the
command cannot be used to find out which doors a machine has.

**A key belongs to one provider.** There is no single `api_key` and no single base url;
`credentials.py` keeps a route per provider — key *and* address in one piece, resolved at
the moment of the call rather than frozen when the reasoner was built, because `/model`
switches providers while the process keeps running. If the selected provider has no key,
the switch is refused and the previous one stays active: no other provider's key stands
in for it. Until 2026-08-05 one field held `ANTHROPIC_API_KEY or OPENAI_API_KEY` for every
provider, so choosing `openai-api` sent an Anthropic key to OpenAI as a bearer token — a
credential disclosure that looks like a typo, because the wrong recipient answers `401`.

**There is no default allowlist.** `TALOS_ALLOWED_PRINCIPALS` must name your identity or
the agent refuses to start. A shipped default would be a backdoor printed in the source.

## How the kernel decides

`PolicyKernel.decide()` returns exactly three verdicts — `ALLOW`, `NEEDS_HUMAN`, `DENY` —
and the executor calls no runner before it has one.

| Class | Examples | Verdict |
|---|---|---|
| Ordinary work | `ls`, `git status`, tests, builds, writes inside the workspace | **runs** |
| Risky but recoverable | `curl \| sh`, `git reset --hard`, `rm -rf <path>` | asks you |
| Persistence & secrets | `.bashrc`, systemd units, `~/.ssh`, the agent's own source | asks you |
| Catastrophic | `rm -rf /`, `mkfs`, `dd` to a block device, fork bomb, reboot | **refused** |
| Secret reads, system paths | `cat /etc/passwd`, anything under `/etc /boot /usr /bin` | **refused** |

`DENY` returns **before** the approval check, so a "yes" cannot reach a hardline rule.
Approvals are one-shot, five-minute TTL, bound to the exact request, and re-verified against
file hashes immediately before execution (TOCTOU).

Targets are derived from real tool arguments, never from a field the model could omit. A
tool without a target extractor is `DENY` by construction.

## The autonomy dial

`/autonomy 0..5` sits above the kernel and **can only tighten, never loosen**. Level 5 is
exactly the unfiltered kernel — not "anything goes". A dial that granted rights would be a
second source of permission next to the kernel, which is the thing this design removes.

| Level | Reads | Writes | Shell |
|---|---|---|---|
| 0 | refused | refused | refused |
| 1 | asks | refused | refused |
| 2 | free | refused | refused |
| 3 | free | asks | asks |
| 4 | free | free in workspace | asks |
| 5 | free | kernel decides | kernel decides |

The level survives restarts because it lives in the event log. An unreadable log drops to
level 0, not to the last convenient value.

## Timed runs

`/every 90 <task>` repeats on an interval; `/every 0 8 * * MON-FRI <task>` takes a cron
expression, because an interval can say "every 90 minutes" but never "weekdays at 08:00".
An expression is a better clock, not an extra permission — what runs afterwards passes the
same kernel.

And it passes one ceiling more. During an unattended run `NEEDS_HUMAN` becomes `DENY`:
what may run without asking runs, everything else is **reported rather than performed**.
Not parked until morning either — an approval question whose occasion is six hours old is
how reflexive clicking starts. So a timed run may do strictly *less* than something you
typed, which is the opposite of how cron usually works.

A blueprint can give a timed run a **memory** and a **probe**. With `continuity: true`
the previous result is placed in front of the task as data — "yesterday 91 %, and
today?" — and a run that ends in the same failure as its predecessor is logged instead
of repeated into the chat. With `monitor: true` and a `probe` (a shell command) the
probe is read *before* the model is asked: an unchanged output means no model call at
all, with `schedule.skipped_unchanged` in the log. The probe is an ordinary `run_shell`
of the task's principal through the same executor, kernel and sandbox, under the same
unattended ceiling — no new permission. And a probe that fails, whatever the reason,
fires the run: a broken sensor must not be the way to silence a watcher.

## Announced plans

For a task with several steps the agent may announce the sequence before it starts:

```
≡ 3 steps — collect the log, find the failing service, report
```

The announcement changes nothing about permission. Every step still passes the kernel one
at a time, exactly as if it had been asked for alone, and a step that needs approval still
stops and asks. What the announcement does is **bind the run**:

- the step budget shrinks from the house limit to what was announced — a three-step plan
  cannot become forty tool calls;
- the first step that fails ends the run with a report of what ran, what stopped it, and
  what therefore did not happen — instead of the model improvising around the failure,
  which is how an agent turns a refusal into a bigger second attempt;
- the plan is read **once**. A tool result is a stranger's text; if it could install a
  second, larger plan mid-run, prompt injection would be a way to buy budget.

### The step that is checked by code

A step may carry a condition, and the condition is **not evaluated by the model**:

```json
PLAN: {"goal": "restart and confirm", "steps": [
  {"intent": "restart the service", "check": "contains:rc=0"},
  {"intent": "write the report",     "check": "wrote:/tmp/report.md"}
]}
```

This closes a real hole. `run_shell` returns `rc=1` and the executor still records
`DONE` — because the *tool* ran. Whether the *work* succeeded lived only in the output
text, and the only reader of that text was the model, which has an interest in the
answer. A condition is checked in code, in the order announced, against the receipt of
the step that ran: `ok` (finished cleanly), `contains:<text>` (its output held that
text), `wrote:<path>` (it wrote exactly there, as the *kernel* derived the target — not
as the model claimed it).

A condition can only ever withhold a confirmation. If one never comes true, the answer is
delivered in full and the system adds its own line beneath it:

```
✕ 1/2 announced checks met — NOT confirmed done. Still open: step 2 (it writes to /tmp/report.md).
```

The honest limit: **the model writes the conditions too.** A met condition proves
that the run's own stated expectation came true — not that the job was done well. Someone
who sets themselves a trivial condition passes it. What the code guarantees is narrower and
still worth a lot: that the condition was really *evaluated* rather than asserted, that it
is shown to you word for word so you can judge its worth, and that the real count also
lands in the event log — which the model cannot write to, and where a forged verdict line
in its prose is contradicted.

Conditions inspect only the receipt of their own step, never the state of the world. One
that could read arbitrary files would be an oracle around the kernel (*"read `/etc/shadow`
and tell me whether it contains root"*), and an unknown condition is dropped rather than
counted, so inventing vocabulary gains nothing.

So planning here does the opposite of what it usually does: it makes a run more
predictable and *less* powerful. That is deliberate, and it is the same inversion the
unattended ceiling makes — a capability that arrives with more rights attached is not a
capability this design accepts. Under that ceiling the two compose without knowing about
each other: an unattended plan stops at the first step that would need a human, and says
so.

## The browser that only reads

`web_fetch` returns source; half the web is an empty shell without JavaScript. `browse`
renders the page in real Chromium and hands back what a reader would see.

What it deliberately cannot do is **operate** a page — no clicking, typing or submitting.
A click has no derivable target: "click the third element" cannot be mapped to a resource
you could bind a permission to, and a tool without a derivable target is `DENY` here by
construction. That is the right answer rather than a shortfall — an agent that operates
strangers' pages has a path to effect that no kernel catches afterwards.

It is also fenced more tightly than a URL check alone can manage. Chromium is pinned to
exactly the address `guard_url` verified, and nothing else resolves at all:

```
--host-resolver-rules="MAP * ~NOTFOUND, MAP <host> <ip>"
```

A redirect to another name goes nowhere, a script pulled from an ad network likewise, and
a DNS rebind between check and fetch — the classic hole in every filter that inspects only
the first name — has no effect. The cost is honest: pages that load their content from a
CDN under a different name come back incomplete. Incomplete beats uncontrolled.

Every call gets a throwaway profile: no cookies, no signed-in sessions, no history. A page
Talos opens sees a factory-fresh browser.

## Delegating

`delegate` hands one self-contained question to a second run — and that run **may only
read**. No writing, no shell, nothing that needs approval. It uses the same executor, the
same kernel and the same identity as its caller; what changes is a fourth ceiling that
turns everything except reading into `DENY` for as long as it lasts.

The reason is the same one that shapes timed runs. A delegated run is born from *model
text*, not from someone typing. Giving it the rights of the run that started it would make
delegation the most convenient way to produce an effect nobody asked for — a second source
of permission wearing a different name. So delegation buys reach, never power: it is for
looking things up without filling the main run's context with the search. What comes back
is data, bounded like any tool result, never an instruction.

## WhatsApp through your own broker

The Cloud-API channel (`whatsapp.py`) can only deliver, because Meta's inbound path is a
webhook — and a webhook is a port the world can reach. The broker channel
(`wabroker.py`) gets inbound without breaking the rule: a listener on **an
operator-controlled WhatsApp broker reachable over SSH** appends the messages routed to
Talos to a JSONL queue, and Talos *pulls* that queue over SSH with a persisted byte
cursor. Every connection goes outward, to a machine the operator controls, with the
operator's own key. Nothing listens.

Replies go back the same way — through the broker's send script, base64-safe; files
travel as `scp` plus broker. The sender number comes out of the operator's own WhatsApp
account rather than a text field, so the channel carries `Trust.FULL`, and the
conversations live in the same `whatsapp:<number>` namespace as before. Three env vars,
all off by default: `TALOS_WA_BROKER_SSH` (the SSH target — empty means the channel does
not exist), `TALOS_WA_BROKER_QUEUE` and `TALOS_WA_BROKER_CLI_DIR`. Configured, the broker
wins over the Cloud-API variant: both are named `whatsapp`, and this is the one that
fetches. A failed poll is a loud `channel.error`, and the cursor never advances on
failure — silence would look exactly like an empty queue. Operator details:
[docs/whatsapp-broker.md](docs/whatsapp-broker.md).

## The MCP registry

MCP is shipped — over the claude-worker seam, not into the agent. Talos itself never
speaks MCP; the `claude -p` child inside a UID-separated worker job does, and Talos only
writes the MCP config and passes it through. Which servers may exist at all is an
operator decision, declared in `data/mcp-servers.json` (gitignored, fail-closed like
`entities.json`): `version: 1` mandatory, absolute commands only, an `env` field costs
the whole entry, sixteen servers at most. The socket frame carries server *names*, never
commands or args.

And it is gated twice: a server must appear in the registry file **and** in the worker's
`TALOS_CLAUDE_WORKER_MCP_SERVERS` allowlist, or the request is refused by name before any
job starts. There is no marketplace and no third-party code in the agent process — a
curated starting point is [examples/mcp-servers.json](examples/mcp-servers.json), the
full contract is [docs/claude-worker.md](docs/claude-worker.md).

## Identity

`SOUL.md` carries the agent's **name and character**. Its first heading is the name —
change `# TALOS` to `# ARGUS`, restart, and the agent is renamed everywhere, including the
header of the live display. There is no second place where the name lives.

`AGENTS.md` carries durable operating discipline; `USER.md` carries stable preferences
for the operator. All three reload on the next message, have explicit context limits, are
protected by the persistence floor, and survive updates as operator-owned state. Missing
`AGENTS.md` or `USER.md` is harmless; a missing or broken `SOUL.md` uses the safe fallback.

## What a run looks like

A plain question gets a plain answer — no status chrome. The moment a tool runs, one
message starts tracking it and stays afterwards as the receipt:

```
◉ Talos · 9s · step 2/100
✓ shell command  2s
▸ write note — disk.md
```

`◉` agent · `◈` thinking · `≡` plan announced · `▸` tool · `✓` done · `✕` failed ·
`⏸` waiting on you · `⛒` refused · `↩` rolled back. One glyph, one meaning, never in the prose of an answer.

## Audit trail

`exec.intent` is written **before** anything happens:

```
exec.intent → approval.parked → approval.granted → exec.intent → exec.result
```

A crash mid-action still leaves a trace. Every run carries the id of the token that
permitted it. `/log` shows the last effects, `/undo` rolls back the last file change.

## Tools

Twenty-three, and every one of them passes the same gate. There is no privileged tool and no
tool that skips the kernel — a tool without a target extractor is `DENY` by construction.

| | |
|---|---|
| `run_shell` | a command, sandboxed, or refused where no sandbox exists |
| `remote_exec` | a command on another machine over ssh — operator-allowlisted hosts, always a human's yes, standing rules bind to exact host+command |
| `http_request` | any REST API — read methods through the SSRF-hardened door, state-changing methods always a human's yes, binding to exact method+URL |
| `git` | clone / fetch / pull / push with credentials — every op a human's yes, binding to exact op+repo+remote; local git work stays in the sandboxed shell |
| `entity_status` | a known name resolved to an operator-configured fixed read-only probe |
| `read_file` / `write_file` / `undo_last` | ordinary work, with a snapshot behind the write |
| `browse` / `web_fetch` / `web_search` | render-only, guarded URL, keyless search by default |
| `see_image` / `grab_frame` / `hear` / `speak` | a picture, one still out of a video, a recording, a voice |
| `vault_search` / `vault_get` / `vault_write_note` | a markdown knowledge base, if you point it at one |
| `session_search` | what was said in earlier turns |
| `delegate` | a sub-run that can only read |
| `delegate_code` / `delegate_dag` / `delegate_status` | a bounded coding job — or a small acyclic graph of them — for a confined Claude worker: opt-in, off by default, writes only into a kernel-derived disposable workspace |
| `delegate_agy` | the same confined worker's second backend (the agy CLI): same trust form and workspace, behind its own opt-in gates on agent and worker, no MCP |
| `delegate_steer` | a course correction into a *running* `/background` task — a framed turn it reads at its next step, never a new right |
| `agent_consult` | bounded advice from a second, operator-configured agent — data, never permission |
| `ask_operator` | the one way it can ask you something on purpose |
| `skill_write` | a new skill, written exactly once — and never without a human's yes |

### Operator-owned entity knowledge

Talos ships with no real entity names, hosts or service units. To enable entity-aware
status checks, copy the neutral example and replace every placeholder with infrastructure
you control:

```bash
mkdir -p data
cp examples/entities.json data/entities.json
```

`data/entities.json` is runtime state: Git ignores it, updates preserve it, and Talos reads
it as context rather than authority. `entity_status` accepts only a configured entity name;
the fixed URL or systemd user unit comes from this file and cannot be supplied by the model.

### Skills it writes for itself

`skill_write` is the one write path into the skills directory — and the hardest gate in the
house. A skill is the strongest persistence there is: its text lands in *every* prompt from
the next turn on. So the tool is declared irreversible, the kernel answers `NEEDS_HUMAN`
without exception, and under the unattended ceiling that becomes `DENY`. It creates only —
never overwrites (atomically, not by looking first), never writes `allowed-tools` (a second
permission source next to the kernel), never anything that looks like a credential, and
never outside the skills root. Improving a skill means the operator deletes it first.

Two blueprints use it on a schedule. `skill-distillation` ("every sunday 20:15") reads the
week's event log and distils at most one repeated, proven workflow into a skill candidate —
written through `skill_write` when a human is there to approve, parked as a vault note under
`patterns/` when the unattended run is refused. And `daily-reflection` closes each day with
a look at the same log — verified live against a real installation, schedules and ceiling
included, not only in the suite.

There is no image generation. That is a decision, not a gap: an agent that can produce
photographs is a different conversation than one that can only look at them.

## Commands

**In a session** — the same set in the terminal and in the messenger, through one
command centre.

`/stop` `/stopall` `/queue` `/status` `/new` `/retry` `/background` · `/pending` `/approve` `/deny`
`/allowed` `/revoke` · `/log` `/undo` `/policy` `/autonomy` `/tools` `/whoami` `/version` ·
`/usage` `/model` `/reasoning` `/debug`

**On the command line** — thirteen, each answering a question an operator actually asks:

| | |
|---|---|
| `chat` | a session here; approvals at a terminal |
| `ask "…"` | one turn, for scripts and cron |
| `setup` · `doctor` · `config` · `models` | set it up, and find out why something won't work |
| `status` · `events` · `why <id>` | what it did, and why that was allowed or refused |
| `report` · `review` | a record someone else can read · what this installation should change |
| `update` · `version` | signature checked, tests first |

`/policy <path|command>` is a dry run: it calls `decide()` and shows the verdict without
executing anything. It is the fastest way to understand the kernel.

## Architecture

Small modules on purpose. The gate path (`policy.py`, 895 lines) has to be readable in one
sitting — a gate you cannot read is not a gate.

| Module | Role |
|---|---|
| `policy.py` | the kernel: target derivation, three verdicts |
| `capability.py` | tokens: mint, redeem, invalidate |
| `command_floor.py` | hardline and dangerous command detection |
| `approval.py` / `standing.py` | one-shot and standing approvals |
| `autonomy.py` / `trust.py` / `channel.py` | ceilings above the kernel |
| `executor.py` / `verifier.py` / `snapshot.py` | write-ahead execution, TOCTOU, undo |
| `conductor.py` / `agent_loop.py` / `worker.py` | orchestration |
| `plan.py` / `schedule.py` / `subagent.py` | announced sequences, timed runs, delegation — all only tighten |
| `web.py` / `browser.py` | the only door out: URL guard, then a resolver cage |
| `reasoner.py` / `provider.py` | pluggable model backends |
| `intelligence.py` / `evaluation.py` | bounded entity context, working state, factual review, adaptive reasoning and trace regression metrics |
| `credentials.py` | one route per provider — key *and* address, resolved at call time |
| `sandbox.py` | bubblewrap / `sandbox-exec`; refuses rather than running unconfined |
| `identity.py` / `ux.py` | name and glyphs |
| `telegram.py` / `mail.py` / `whatsapp.py` / `wabroker.py` | channels — every way in fetches, none listens |
| `vision.py` / `hearing.py` / `speech.py` / `frames.py` | reading a picture, hearing a recording, speaking, one still out of a video — ordinary READ/WRITE with a target |
| `cli.py` / `doctor.py` / `configcli.py` / `schema.py` | the subcommands: diagnose, read and change settings — the schema decides what may be written, and the allowlist and the network exceptions never are |
| `askcli.py` | `talos ask` — one turn from a script, as a channel with no special right |
| `models.py` | live model lists, cached on disk, added to the curated catalogue and never replacing it |
| `updater.py` | update beside the old tree, both suites in the new one, switch only if green |
| `eventlog.py` / `memory.py` / `usage.py` | durable log, conversation memory, metering |
| `mcpservers.py` / `skillwrite.py` | the operator-owned MCP registry, and the one gated write into the skills directory |

## Roadmap

1. `openat2()` with `RESOLVE_BENEATH` instead of realpath checks.
2. **An adapter seam for tools.** Adding a channel is already just implementing a
   protocol; adding a *tool* still touches three places, one of them inside the kernel
   (`TARGET_EXTRACTORS`). The seam should make a new effect routine without making the
   kernel optional — everything still passes the same gate, the same tokens, the same
   snapshots, the same receipts.
3. **Ownership as the boundary, not just the floor.** While the agent and its config file
   belong to the same writable user, the floor and the sandbox are code in the same
   process. A separate user for the model worker would make it a real separation. On a
   hardened installation the config already belongs to root and the agent may only read it.

Landed since this list was first written: the sandbox for `run_shell` (bubblewrap /
`sandbox-exec`, see [What it does not do](#what-it-does-not-do)), streaming replies,
timed runs under a ceiling that makes them weaker than typed ones,
[announced plans](#announced-plans) built so that planning tightens a run instead of
widening it, read-only delegation, a render-only browser, the media tools, hearing on the
machine itself, one still out of a video, keyless search, **mail as a second way in**,
a command line that grants nothing by being one, **MCP over the claude-worker seam**
(an [operator-owned registry](#the-mcp-registry) instead of a marketplace),
[WhatsApp through an operator-controlled broker](#whatsapp-through-your-own-broker), and
[skills the agent may write — but never alone](#skills-it-writes-for-itself).

## Contributing

Changes to the kernel (`policy.py`, `capability.py`, `command_floor.py`, `approval.py`,
`standing.py`, `autonomy.py`, `trust.py`, `verifier.py`, `executor.py`) need:

1. `pytest`, `redteam.py` and `e2e.py` green, in that order.
2. **Any loosening must add a red-team case** proving the boundary next to it still holds.

If you find a way past the kernel, that is the most valuable thing you can contribute —
and it belongs in the **private** channel, not in a public issue: see
[SECURITY.md](SECURITY.md), which also states the scope and the limits no patch will
remove. Everything else — a missing tool, a refusal you disagree with, a wrong answer —
is a normal issue and welcome as one.

## License

MIT. See [LICENSE](LICENSE).
