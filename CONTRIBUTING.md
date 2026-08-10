# Contributing to Talos

Talos is a small, auditable security kernel for an autonomous agent: the model proposes,
the kernel decides, the executor performs. That order is the product. A contribution is
welcome exactly to the degree that it keeps that order intact and provable.

This guide expands the short version in the [README](README.md#contributing) and the
change protocol in [CLAUDE.md](CLAUDE.md). Read [SECURITY.md](SECURITY.md) before touching
anything security-relevant — it states the scope and the limits no patch will remove.

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Three suites, and they are the point — Talos sells that its claims are checked, so a change
is not done until they are green:

```bash
python -m pytest tests/ -q     # the unit suite
python redteam.py              # adversarial cases against the real kernel — mandatory for any kernel change
python e2e.py                  # the full path against a real model (costs tokens and time)
python -m talos --once         # one real cycle, after wiring anything new
python -m talos doctor         # what this machine is missing — changes nothing
```

## Where a first contribution belongs

The kernel is deliberately hard to change (see below). The rewarding first work is in the
periphery that surrounds it, where a mistake costs a test rather than the security model:

- **Docs** — a rule in `CLAUDE.md` that reads as counterintuitive and cost you an hour;
  the place it tripped you is where the note belongs.
- **Tests** — a branch the suite does not cover, an edge case a tool mishandles.
- **Provider adapters / model catalogue** — a new backend the reasoner can drive.
- **CLI and console polish** — clearer `doctor` output, a better `--help`, a read-only view.
- **A tool** — but every tool is gated; a tool without a target extractor is `DENY` by
  construction, and that is not negotiable. Open an issue first to agree the shape.

Good first issues are labelled as such. If none fit, open an issue describing what you want
to do before you write it — for anything non-trivial that conversation saves you a rewrite.

## Changing the kernel

Applies to `policy.py`, `capability.py`, `command_floor.py`, `approval.py`, `standing.py`,
`autonomy.py`, `trust.py`, `verifier.py`, `executor.py`, and the ceilings above them
(`schedule.py`, `plan.py`, `subagent.py`). The bar is high on purpose:

1. Branch; never commit straight to the default branch.
2. `pytest`, then `redteam.py`, then `e2e.py` — all green, in that order.
3. **Any loosening must add a red-team case** proving the boundary next to it still holds.
4. Say plainly in the PR what the change makes *possible* that was not possible before. A
   security change whose blast radius is not written down is not reviewable, and will not
   be merged.

The gate path (`policy.py`) must stay readable in one sitting. A change that makes it longer
without making it clearer is moving the wrong way.

## Found a way past the kernel?

That is the most valuable thing you can contribute — and it does **not** go in a public
issue. Use the private channel in [SECURITY.md](SECURITY.md). Everything else — a missing
tool, a refusal you disagree with, a wrong answer — is a normal issue and welcome as one.

## Style

- Comments and docstrings explain **why**, especially where a rule looks counterintuitive —
  those are the ones argued away six months later.
- Small modules, small functions. Feature-based organisation.
- The machine console (status display, `/help`, kernel reasons) stays English; user-facing
  conversation follows the user's language.
- Conventional-commit subjects (`feat(...)`, `fix(...)`, `docs: ...`). One logical change
  per commit; each leaves the suites green.

## What a good PR looks like

Small and focused. A clear title. A description that says what changed and why, references
the issue it closes, and — for anything near the kernel — names the blast radius. Green CI.
If it touches user-facing text that `redteam.py` or `e2e.py` assert on, it updates them in
the same commit.

By contributing you agree your work is licensed under the project's [MIT license](LICENSE).
