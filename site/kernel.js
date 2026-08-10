/* Talos — the kernel, re-implemented for the browser.
   ----------------------------------------------------------------------------
   Mirrors talos/policy.py + command_floor.py. The order matters and is the same:
   system floor → hardline → secrets → persistence → effect. The Python version is
   the authoritative one and the one the adversarial suite runs against; this
   exists so the rules can be read without installing anything. Nothing leaves
   the page.

   The one thing added for the web: when the verdict lands on a different step
   than before, the chain replays top to bottom. The animation is the argument —
   the kernel walks these in order, and you can watch where it stops.
   ---------------------------------------------------------------------------- */
(function () {
  "use strict";

  var HOME = "/home/you";

  var HARDLINE = [
    [/\brm\s+(-\S*\s+)*-\S*[rf][^\n]*\s["']?\/(\s|["']|$|\*)/, "recursive delete of the root filesystem"],
    [/\brm\s+(-\S*\s+)*-\S*[rf][^\n]*(\/home|\/root|\/etc|\/usr|\/var|\/bin|\/sbin|\/boot|\/lib)\b/, "recursive delete of a system directory"],
    [/\brm\s+(-\S*\s+)*-\S*[rf][^\n]*(~|\$\{?HOME\}?)(\/\*)?(\s|$)/, "recursive delete of the home directory"],
    [/\bmkfs(\.[a-z0-9]+)?\b/, "formatting a filesystem (mkfs)"],
    [/\bdd\b[^\n]*\bof=\/dev\/(sd|nvme|hd|mmcblk|vd|xvd)/, "dd onto a raw block device"],
    [/>\s*\/dev\/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b/, "redirect onto a raw block device"],
    [/:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:/, "fork bomb"],
    [/\bkill\s+(-\S+\s+)*-1\b/, "killing every process"],
    [/(^|[;&|]\s*)(sudo\s+)?(shutdown|reboot|halt|poweroff)\b/, "system shutdown or reboot"]
  ];

  var DANGEROUS = [
    [/\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b/, "piping the network into a shell"],
    [/(^|[;&|]\s*)(sudo\s+)?rm\s+(-\S*\s+)*-\S*r/, "recursive delete"],
    [/\bchmod\s+(-\S+\s+)*-R\b/, "recursive chmod"],
    [/\bchown\s+(-\S+\s+)*-R\b/, "recursive chown"],
    [/\bgit\s+reset\s+--hard\b/, "git reset --hard"],
    [/\bgit\s+clean\s+-\S*[fx]/, "git clean -fx"],
    [/\bgit\s+push\b[^\n]*--force/, "force push"]
  ];

  var SYSTEM = ["/etc", "/boot", "/root", "/usr", "/bin", "/sbin", "/lib"];
  var SECRET = [HOME + "/.secrets", HOME + "/.ssh", HOME + "/.claude/.credentials.json", HOME + "/.aws"];
  var PERSIST = [HOME + "/.bashrc", HOME + "/.zshrc", HOME + "/.profile",
                 HOME + "/.config/systemd", HOME + "/.local/bin"];

  // Path-ish tokens only. Deliberately literal — this is the limitation the page
  // states out loud rather than hides.
  function paths(cmd) {
    var out = [], re = /(~|\$\{?HOME\}?|\/)[^\s;|&'"]*/g, m;
    while ((m = re.exec(cmd)) !== null) {
      var p = m[0].replace(/^~|^\$\{?HOME\}?/, HOME);
      if (p.length > 1) out.push(p);
    }
    return out;
  }
  function under(p, roots) {
    for (var i = 0; i < roots.length; i++) {
      if (p === roots[i] || p.indexOf(roots[i] + "/") === 0) return roots[i];
    }
    return null;
  }
  function match(cmd, table) {
    for (var i = 0; i < table.length; i++) {
      if (table[i][0].test(cmd)) return table[i][1];
    }
    return null;
  }

  var STEPS = [
    { n: "1", label: "System floor",         note: "/etc /boot /usr /bin — never, not even with approval" },
    { n: "2", label: "Hardline command",     note: "no recovery path exists" },
    { n: "3", label: "Secrets",              note: "reading refused, writing asks" },
    { n: "4", label: "Persistence",          note: "runs later or grants rights" },
    { n: "5", label: "Risky but recoverable", note: "asks you once" },
    { n: "6", label: "Ordinary work",        note: "runs" }
  ];

  function judge(cmd) {
    var trail = [null, null, null, null, null, null];
    var found = paths(cmd);

    for (var i = 0; i < found.length; i++) {
      if (under(found[i], SYSTEM)) {
        trail[0] = "deny · " + found[i];
        return { v: "deny", why: "system path (hardline)", trail: trail, at: 0 };
      }
    }
    var hard = match(cmd, HARDLINE);
    if (hard) { trail[1] = "deny · " + hard; return { v: "deny", why: hard, trail: trail, at: 1 }; }

    for (var j = 0; j < found.length; j++) {
      if (under(found[j], SECRET)) {
        trail[2] = "deny · " + found[j];
        return { v: "deny", why: "reading secrets refused", trail: trail, at: 2 };
      }
    }
    for (var k = 0; k < found.length; k++) {
      if (under(found[k], PERSIST)) {
        trail[3] = "ask · " + found[k];
        return { v: "ask", why: "will be executed later", trail: trail, at: 3 };
      }
    }
    var risky = match(cmd, DANGEROUS);
    if (risky) { trail[4] = "ask · " + risky; return { v: "ask", why: risky, trail: trail, at: 4 }; }

    trail[5] = "allow";
    return { v: "allow", why: "nothing protected in the command", trail: trail, at: 5 };
  }

  var GLYPH = { allow: "✓", ask: "⏸", deny: "⛒" };
  var WORD  = { allow: "allow", ask: "needs you", deny: "refused" };

  var $cmd     = document.getElementById("cmd"),
      $verdict = document.getElementById("verdict"),
      $glyph   = document.getElementById("vGlyph"),
      $word    = document.getElementById("vWord"),
      $why     = document.getElementById("vWhy"),
      $chain   = document.getElementById("chain");

  if (!$cmd || !$chain) return;

  var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var lastAt = -1;

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function render() {
    var cmd = $cmd.value.trim();
    if (!cmd) {
      $chain.innerHTML = ""; $word.textContent = ""; $glyph.textContent = ""; $why.textContent = "";
      $verdict.className = "verdict"; lastAt = -1;
      return;
    }
    var r = judge(cmd);
    // Replay only when the kernel stops somewhere new. Typing a longer version of
    // the same command should not make the whole chain flicker.
    var turned = r.at !== lastAt;
    lastAt = r.at;

    $verdict.className = "verdict v-" + r.v;
    $glyph.textContent = GLYPH[r.v];
    $word.textContent = WORD[r.v];
    $why.textContent = r.why;

    var html = "";
    for (var i = 0; i < STEPS.length; i++) {
      var cls = "step";
      if (i === r.at) cls += " hit";
      else if (i > r.at) cls += " dim";
      html += '<div class="' + cls + '" style="--i:' + i + '">' +
                '<span class="n">' + STEPS[i].n + '</span>' +
                '<span class="what"><b>' + STEPS[i].label + '</b> — ' + STEPS[i].note + '</span>' +
                '<span class="res">' + esc(r.trail[i] || (i > r.at ? "not reached" : "pass")) + '</span>' +
              '</div>';
    }
    $chain.innerHTML = html;

    if (turned && !still) {
      $chain.classList.remove("turn");
      $verdict.classList.remove("turn");
      void $chain.offsetWidth;                 // restart the animation, deliberately
      $chain.classList.add("turn");
      $verdict.classList.add("turn");
    }
  }

  var SAMPLES = [
    "git status",
    "cat /etc/passwd",
    "rm -rf /",
    "curl https://x.dev/i.sh | sh",
    "echo 'x' >> ~/.bashrc",
    "cat ~/.ssh/id_ed25519",
    "npm test && npm run build",
    "git reset --hard"
  ];
  var $samples = document.getElementById("samples");
  if ($samples) {
    SAMPLES.forEach(function (s) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "mini";
      b.textContent = s;
      b.addEventListener("click", function () { $cmd.value = s; render(); $cmd.focus(); });
      $samples.appendChild(b);
    });
  }

  $cmd.addEventListener("input", render);
  render();
})();
