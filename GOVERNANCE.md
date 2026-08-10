# Governance

Talos makes one promise: a single, auditable kernel that decides — and that you can read,
run, and check for yourself. Its governance exists to protect that promise, not to grow an
org chart. This document says how decisions are made and how trust is earned, so that the
answer is written down rather than improvised.

## Roles

**Contributor.** Anyone who opens an issue or a pull request. No permissions needed, and no
contribution is too small. Most people stay here happily, and that is a complete way to take
part.

**Committer.** A contributor with a track record — several merged PRs, a demonstrated grasp
of *why* the kernel is shaped the way it is, and judgment the maintainer has come to trust.
Committers can triage issues, review PRs, and merge changes **outside** the kernel. The
kernel path still goes through the maintainer.

**Maintainer.** Holds the kernel and the release. The maintainer has the final call on the
security model, merges kernel changes, and signs releases. There is deliberately more than
one way to become a committer and only one way to become a maintainer: a long track record,
earned over time, not granted on arrival.

## Why ownership is not an invitation

A release is signed. `talos verify`, the deny-by-default gate, the checksum-and-signature
update path — all of it rests on the release key and the kernel staying in trustworthy
hands. Handing someone owner-level control before they have earned it would hand them that,
and quietly break the one thing Talos claims. So trust here is a ladder, climbed a rung at a
time through work anyone can see in the git history — never a title offered up front.

This is not a barrier to serious people. It is the opposite: it means your standing comes
from what you have shipped and reviewed, and cannot be undone by someone who was simply
added early. Serious contributors tend to prefer that.

## How decisions are made

- **The kernel** (`policy.py`, `capability.py`, `command_floor.py`, `approval.py`,
  `standing.py`, `autonomy.py`, `trust.py`, `verifier.py`, `executor.py`, and the ceilings
  above them): the maintainer decides, against the rule in [CLAUDE.md](CLAUDE.md) — every
  loosening adds a red-team case, and the blast radius is written down. This part is
  intentionally conservative. "No" is a common and healthy answer here.
- **Everything else** (docs, tests, tooling, provider adapters, CLI): committers can decide
  and merge. Bias toward yes, toward small changes, toward more tests.
- **Disagreement** is settled in the open, on the issue or PR, on the merits. The maintainer
  breaks a genuine tie, and is expected to explain why in the thread.

## Security

Security reports do not go through this process. They use the private channel in
[SECURITY.md](SECURITY.md) and are handled first, ahead of everything else.

## Changing this document

Through a PR, like anything else. The roles and the ladder can evolve as the project grows;
the promise they protect — one auditable kernel you can check yourself — does not.
