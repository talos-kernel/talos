# Website

Static, single file, no build step and no dependencies. Deploy by copying this
directory to a webroot.

`index.html` carries a browser re-implementation of the policy kernel so visitors can
watch it judge their commands without installing anything. It mirrors the decision order of
`talos/policy.py` and `talos/command_floor.py`.

**If you change the kernel, re-check the demo.** The Python version is authoritative;
a demo that disagrees with it is worse than no demo. The verdicts were verified against
the real kernel for: ordinary commands, hardline patterns, system paths, secret paths,
persistence targets and recoverable-but-risky commands.
