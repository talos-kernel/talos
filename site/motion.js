/* Talos — motion.
   ----------------------------------------------------------------------------
   Everything in here is decoration. The page is complete and readable without
   it: `style.css` only hides something once <html> carries the `js` class, and
   the inline head snippet that sets that class also removes it again if this
   file never arrives. Content is never one failed request away from invisible.

   Rules followed here:
   - `prefers-reduced-motion` is checked once, in JS as well as in CSS. Under it
     nothing is staggered and nothing counts up; final states are set directly.
   - Observers disconnect after firing. A reveal that keeps watching is a leak.
   - No layout is read in a scroll handler. The only scroll listener flips one
     class and is passive.
   ---------------------------------------------------------------------------- */
(function () {
  "use strict";

  var root = document.documentElement;
  root.setAttribute("data-motion", "on");           // tells the head failsafe we made it

  var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var canObserve = "IntersectionObserver" in window;

  /* ── theme ───────────────────────────────────────────────────────────────
     The stored choice is applied by the head snippet so the first paint is
     already correct. Here we only handle the toggle and keep the label true. */
  var toggle = document.getElementById("themeBtn");
  function currentTheme() {
    var set = root.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  function labelTheme() {
    if (!toggle) return;
    var next = currentTheme() === "dark" ? "light" : "dark";
    toggle.setAttribute("aria-label", "Switch to " + next + " mode");
    toggle.setAttribute("title", "Switch to " + next + " mode");
  }
  if (toggle) {
    labelTheme();
    toggle.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("talos-theme", next); } catch (e) { /* private mode */ }
      labelTheme();
    });
  }

  /* ── nav gains its edge once the page has moved ──────────────────────────── */
  var nav = document.querySelector("nav");
  if (nav) {
    var pinned = false;
    var onScroll = function () {
      var now = window.scrollY > 8;
      if (now !== pinned) { pinned = now; nav.classList.toggle("scrolled", now); }
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ── reveal ──────────────────────────────────────────────────────────────
     Siblings inherit a stagger from their position, capped so a long grid
     never turns into a queue you have to wait out.                          */
  var targets = [].slice.call(document.querySelectorAll("[data-reveal]"));
  targets.forEach(function (el) {
    var sibs = [].slice.call(el.parentNode.children).filter(function (n) {
      return n.hasAttribute && n.hasAttribute("data-reveal");
    });
    el.style.setProperty("--i", Math.min(sibs.indexOf(el), 6));
  });

  function showAll() { targets.forEach(function (el) { el.classList.add("in"); }); }

  if (still || !canObserve) {
    showAll();
  } else {
    var revealer = new IntersectionObserver(function (entries, obs) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("in");
        obs.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.06 });
    targets.forEach(function (el) { revealer.observe(el); });
  }

  /* ── the guardian shows up ───────────────────────────────────────────────
     install.sh prints the mask one line at a time (`sleep 0.035`). The page
     does the same thing at the same pace — it is a preview of the terminal,
     not an invented effect.                                                  */
  var rows = [].slice.call(document.querySelectorAll(".mask .row"));
  function drawMask() {
    rows.forEach(function (row, i) {
      setTimeout(function () { row.classList.add("lit"); }, 220 + i * 42);
    });
  }
  if (rows.length) {
    if (still) { rows.forEach(function (r) { r.classList.add("lit"); }); }
    else { drawMask(); }
  }

  /* ── the numbers count ───────────────────────────────────────────────────── */
  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-count"));
    var suffix = el.getAttribute("data-suffix") || "";
    if (isNaN(target)) return;
    var dur = 900, t0 = null;
    function frame(t) {
      if (t0 === null) t0 = t;
      var p = Math.min((t - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);              // ease-out cubic
      el.textContent = Math.round(target * eased) + suffix;
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  var numbers = [].slice.call(document.querySelectorAll("[data-count]"));
  if (still || !canObserve) {
    numbers.forEach(function (el) {
      el.textContent = el.getAttribute("data-count") + (el.getAttribute("data-suffix") || "");
    });
  } else {
    var counter = new IntersectionObserver(function (entries, obs) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        countUp(e.target);
        obs.unobserve(e.target);
      });
    }, { threshold: 0.5 });
    numbers.forEach(function (el) { counter.observe(el); });
  }

  /* ── copy buttons ────────────────────────────────────────────────────────── */
  [].slice.call(document.querySelectorAll("[data-copy]")).forEach(function (btn) {
    var source = document.getElementById(btn.getAttribute("data-copy"));
    if (!source) return;
    btn.addEventListener("click", function () {
      var text = source.innerText.trim();
      var settle = function (word) {
        btn.textContent = word;
        btn.classList.add("done");
        setTimeout(function () { btn.textContent = "copy"; btn.classList.remove("done"); }, 1600);
      };
      if (!navigator.clipboard) { btn.textContent = "select it"; return; }
      navigator.clipboard.writeText(text).then(function () { settle("copied"); },
                                               function () { btn.textContent = "select it"; });
    });
  });

  /* ── the patrol band ─────────────────────────────────────────────────────
     One set of cases is in the markup; the loop needs a second identical set
     so the -50% wrap is seamless. Duplicating in JS rather than in HTML keeps
     the source honest: there is exactly one list of cases to maintain, and a
     screen reader is never read the same line twice.                        */
  var track = document.querySelector(".patrol-track");
  if (track && !still) {
    var set = track.firstElementChild;
    if (set) {
      var clone = set.cloneNode(true);
      clone.setAttribute("aria-hidden", "true");
      track.appendChild(clone);
    }
  }
})();
