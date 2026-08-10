# Security Policy

Talos is an agent whose actions are bounded by a deterministic permission kernel. A
project that makes that claim owes you two things: a way to tell us when the claim fails,
and an honest account of where it does not apply in the first place. Both are below.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:**
[Report a vulnerability](https://github.com/talos-kernel/talos/security/advisories/new)

That channel is private until we publish an advisory together. Please use it rather than
a public issue for anything that could be exploited before a fix exists.

There is deliberately no email address here. A published address is a permanent target and
a permanent piece of personal data; the reporting form gives you the same private channel
without either.

**What helps:**

- The version (`python -m talos --version`, or the `__version__` in `talos/__init__.py`).
- The path: which channel, which identity level, which autonomy level, and — if the kernel
  was involved — the verdict you saw.
- A reproduction. `talos why <run-id>` prints the verdict, the rule that produced it and
  the targets it applied to; that output is usually the most useful thing you can attach.
- What you expected the kernel to do instead.

**What to expect:**

| | |
|---|---|
| Acknowledgement | within 5 days |
| First assessment | within 14 days |
| Fix or a stated reason not to | within 90 days for anything we accept |
| Credit | offered by name or handle, or omitted — your choice |

This is a one-maintainer alpha project. Those are the targets we intend to meet, not a
contractual guarantee, and if a date is going to slip you will be told rather than left
waiting.

## Supported versions

| Version | Supported |
|---|---|
| Latest published release | ✅ |
| Anything older | ❌ |

Versions are alpha: the kernel's rules are stable, the surface around them is not. There
is no backporting. `talos update` moves an installation to the published version after
proving both suites in a new tree beside the old one, and it is the intended path.

## Scope

**In scope — these are the promises, so a break in any of them is a vulnerability:**

- **The kernel decides, the model proposes.** Anything that lets model-produced text
  cause an effect without passing `policy.py` / the capability mint.
- **Path floor.** Any read or write outside the permitted roots, including via symlink,
  `..`, a shell detour, or a tool's own arguments.
- **Identity.** Any way for a principal outside the allowlist to command the agent, or for
  one allowed principal to act as another.
- **Approvals.** Anything that turns a refusal into an execution: a forged token, a
  standing approval that covers more than it names, a reply landing on the wrong pending
  action.
- **The unattended ceiling.** Anything that lets a scheduled or background run take an
  action that would have required a human.
- **Release integrity.** Anything that lets an unsigned or altered archive be installed or
  updated to — including a downgrade of the checks themselves.
- **Secret handling.** Tokens or keys reaching a log, a transcript, a summary, a channel,
  or the model's context.

**Out of scope:**

- The model being wrong, evasive, or verbose. That is not a security boundary — it is why
  every effect goes through the kernel, and why `outcome.py` states which tools actually
  failed next to whatever the model said about it.
- Prompt injection *reaching* the model. Assumed, not prevented; see below.
- Vulnerabilities in a provider's CLI or API, in Python, or in a dependency — report those
  upstream. If Talos uses one in a way that makes it worse, that part is in scope.
- Denial of service by an *allowed* principal (they can already run the agent).
- Anything requiring an attacker who already has the operator's account, the machine, or
  root on it.

## Verifying what you downloaded

Every release is a gzipped tarball with a SHA-256 sum and an Ed25519 signature. The public
key ships **inside the code** (`RELEASE_PUBLIC_KEY` in `talos/updater.py`) and therefore
does not live on the server it protects. The private half is offline and is never on the
release host.

```bash
V=0.9.2-alpha
BASE=https://talos-agent.ch/dist
curl -fsSLO $BASE/talos-$V.tar.gz
curl -fsSLO $BASE/talos-$V.tar.gz.sha256
curl -fsSLO $BASE/talos-$V.tar.gz.sig

# 1. Did it arrive intact?
shasum -a 256 -c talos-$V.tar.gz.sha256    # or: sha256sum -c

# 2. Did it come from us? (needs `pip install cryptography`)
python3 - <<'PY'
import base64, glob, pathlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
KEY = "Do7lfPckC7pJJtD4BECN/mLPIOqHZVWm/j/MfJOK2hk="
tar = pathlib.Path(glob.glob("talos-*.tar.gz")[0]).read_bytes()
sig = base64.b64decode(pathlib.Path(glob.glob("talos-*.sig")[0]).read_text().strip())
Ed25519PublicKey.from_public_bytes(base64.b64decode(KEY)).verify(sig, tar)
print("signature OK")
PY
```

The checksum answers "did it arrive intact"; the signature answers "did it come from us".
They are different questions, and the second one matters because the sum sits next to the
archive — whoever can replace one can replace both.

`install.sh` and `talos update` both perform these two checks and **refuse** on failure;
neither merely displays them. Both run before anything is unpacked and long before any
code from the archive is executed.

## Threat model, stated plainly

**Assumed hostile:** everything the model reads. Web pages, tool output, file contents,
mail, message text, an earlier conversation. Prompt injection is treated as a given, not
as something to be filtered out. The defence is that model text cannot *do* anything: it
proposes, and a separate deterministic kernel decides.

**Assumed honest:** the operator, the machine, the Python interpreter, and the allowlist.
Talos does not defend against its own operator, against local root, or against a
compromised messenger account — a principal in the allowlist *is* the person.

**Deliberately absent:** there is no second source of permission. No config file, no
gateway, no portal, no environment variable can grant what the kernel refuses. Tests
assert this against the module sources rather than trusting the documentation.

## Known limitations — accepted, not hidden

These are real, they are not going to be fixed by a patch, and you should decide with them
in view rather than discover them later.

- **A self-updater cannot escape its own trust chain.** Verification runs in the tree that
  is *already installed*. Anyone who once gets a correctly signed malicious update owns
  every update after it, because that update replaces the pinned key too. Signing is what
  makes that hard; it is not what makes it impossible.
- **One release key.** If the private key is lost, no new version can be signed, and every
  installation from 0.7.0 onward will refuse to update — there is no fallback to
  "checksum only", because that fallback is exactly the path an attacker takes.
- **Shell isolation depends on `bubblewrap`.** Where it is absent the shell is weaker than
  the documentation implies. `talos doctor` says so rather than pretending otherwise; read
  it before you rely on the sandbox.
- **A step limit is a brake, not a budget.** A run may take up to a hundred kernel-checked
  actions. Each one is judged individually, but "many small permitted actions" is a real
  shape of damage that no single verdict catches.
- **Approval fatigue is a vulnerability class we can only mitigate.** A prompt that appears
  often enough gets a reflexive yes. `review` reports prompts worn out by repetition; it
  cannot stop a human from clicking.
- **A correction can steer a run in progress.** It carries no additional rights and is
  refused unless it comes from the same principal in the same conversation and no approval
  is open — but it does change what a running task does.
- **The event log is the record, and it lives on the same machine.** Each entry now carries
  the hash of the one before it (a chain), and `talos verify` recomputes it: editing a past
  entry or deleting one from the middle breaks the chain at that point and is named. What it
  still cannot see, and does not pretend to: anyone with local root can recompute the *whole*
  chain from scratch, and cutting entries off the *end* leaves a shorter chain that is
  internally consistent — catching that needs a head anchored outside this machine, which
  this version does not have. Talos is a guardian, not an auditor of its own host.

## What is not a vulnerability report

A model output that is wrong, a refusal you disagree with, or a tool that is missing. Those
are issues, and they are welcome as issues — but they do not need a private channel, and
filing them there slows down the reports that do.
