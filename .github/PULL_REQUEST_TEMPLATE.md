<!-- Keep it small and focused. One logical change per PR. -->

## What changed and why

<!-- The why matters more than the what. If it touches the kernel, name the blast radius:
     what does this make possible that was not possible before? -->

Closes #

## Checklist

- [ ] `pytest tests/ -q` is green
- [ ] `redteam.py` is green (run it for any kernel-adjacent change)
- [ ] `e2e.py` is green, or I say why it was not run
- [ ] This touches the kernel (`policy.py`, `capability.py`, `command_floor.py`,
      `approval.py`, `standing.py`, `autonomy.py`, `trust.py`, `verifier.py`, `executor.py`,
      or a ceiling) — and if it **loosens** anything, I added a red-team case proving the
      boundary next to it still holds.
- [ ] User-facing text that `redteam.py`/`e2e.py` assert on is updated in the same commit.
- [ ] Docstrings explain the *why* where a rule looks counterintuitive.

<!-- Found a way PAST the kernel? Do not open this as a PR or a public issue.
     Use the private channel in SECURITY.md. -->
