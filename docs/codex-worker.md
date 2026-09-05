# Codex worker

`delegate_codex {"prompt": "…"}` sends a bounded coding or review task through the
same local socket, kernel and outer sandbox as `delegate_code`. `delegate_status`
reads the result; terminal jobs also notify the conversation that started them.

For the richer Telegram activity display, set `TALOS_STATUS_STYLE=expressive` in the
agent environment. It shows measured time, tool calls and the current state. A
finished conversation turn does not imply a delegated worker job has finished.

Enable both sides explicitly:

```sh
# Agent environment, alongside the existing worker switch and socket:
TALOS_CODEX_BACKEND=1

# Worker service environment:
TALOS_CLAUDE_WORKER_CODEX_BIN=/usr/bin/codex
TALOS_CLAUDE_WORKER_CODEX_HOME=/var/lib/talos/codex-home
# Optional: leave unset to use the installed CLI's default model.
# TALOS_CLAUDE_WORKER_CODEX_MODEL=<model-id>
```

Install Codex separately. Authenticate as the worker user with `CODEX_HOME` pointing
at the configured directory. Protect it with mode 0700 and its `auth.json` with 0600.
Credentials remain operator-owned state and never belong in the repository.
No auth file, invalid binary or missing worker configuration means `unavailable`.

Each job copies only `auth.json` into a fresh `.home/.codex` inside its workspace.
It inherits no operator configuration, skills, MCP servers or hooks. The source
credential directory is not in argv or the child's environment; the job credential
copy is removed after success, failure or timeout. A forced worker crash can leave
job state behind, so protect the worker root and remove abandoned workspaces during
maintenance. Auth refreshes within a job are not copied back; renew the worker login
when the source credential expires.

Codex runs `exec --json --ephemeral --sandbox danger-full-access` with non-interactive
approvals **inside the mandatory Talos sandbox**. The inner CLI sandbox is disabled:
its nested bubblewrap cannot build mount targets under the outer read-only root on
Linux. Talos still confines the process to its job workspace, masks protected paths
and refuses to spawn when no outer sandbox exists. Never run this invocation outside
the worker. It grants no host-wide write access and has no unconfined fallback.
See the [official non-interactive mode documentation](https://developers.openai.com/codex/noninteractive/).

An exit code of zero alone is insufficient: the stream must contain
`turn.completed`. `turn.failed` is a failure even with exit zero. File evidence
comes from completed `file_change` items and remains within the workspace; a path
in prose is not evidence. A successful worker receipt still needs the appropriate
artifact or test read-back before Talos claims the user's task is complete.

Use Claude for enabled browser/MCP tasks, Codex for implementation or independent
review, and Antigravity as another configured backend. Give each a precise task,
relevant context, writable scope and checks. Jobs do not share a workspace implicitly.

Dependency audit (checks the shipped pins, including transitive packages):

```sh
osv-scanner scan source --no-resolve \
  --lockfile requirements.txt:requirements.lock \
  --lockfile requirements.txt:requirements-dev.lock
```
