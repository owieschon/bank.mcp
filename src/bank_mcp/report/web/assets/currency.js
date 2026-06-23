/* currency.js — optional USD↔secondary-currency toggle for bank.mcp reports.
 *
 * No backend. Every money figure is rendered server-side in USD with a
 * data-usd="<numeric>" attribute. This module:
 *   1. Reads a build-time config from window.__FX (baked by the Python pipeline):
 *      { rate, ppp, date, ccy, locale }. If `ccy` is null/absent, no secondary
 *      currency is configured — the report stays USD-only and the toggle hides.
 *   2. Otherwise, on load, refreshes the rate live from a free, no-key,
 *      CORS-enabled FX API (open.er-api.com, then frankfurter.dev), falling back
 *      to the baked rate if both are unreachable.
 *   3. Converts every [data-usd] element on toggle, persisting the choice.
 *
 * Deterministic math stays in Python; this only re-denominates display values.
 * The secondary currency is configured via env at build time (REPORT_SECONDARY_*),
 * so nothing about a particular locale is hardcoded here.
 */
(function () {
  "use strict";

  var baked = window.__FX || {};
  var SECONDARY = (typeof baked.ccy === "string" && baked.ccy) ? baked.ccy.toUpperCase() : null;

  // No secondary currency configured → leave the server-rendered USD as-is and
  // keep the (hidden) toggle hidden. Expose a no-op API for callers.
  if (!SECONDARY || typeof baked.rate !== "number") {
    window.FX = { setCurrency: function () {}, get rate() { return 1; }, get currency() { return "USD"; } };
    return;
  }

  var rate = baked.rate;
  var rateDate = baked.date || null;
  var ppp = typeof baked.ppp === "number" && baked.ppp > 0 ? baked.ppp : null;
  var locale = baked.locale || "en-US";
  var isLive = false;

  // Live sources, tried in order. Each returns USD->SECONDARY.
  var SOURCES = [
    { url: "https://open.er-api.com/v6/latest/USD",
      pick: function (d) { return d && d.rates && d.rates[SECONDARY]; },
      when: function (d) { return d && d.time_last_update_utc; } },
    { url: "https://api.frankfurter.dev/v1/latest?base=USD&symbols=" + SECONDARY,
      pick: function (d) { return d && d.rates && d.rates[SECONDARY]; },
      when: function (d) { return d && d.date; } }
  ];

  var fmtUSD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  var fmtUSD0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  var fmtSEC = new Intl.NumberFormat(locale, { style: "currency", currency: SECONDARY });
  var fmtSEC0 = new Intl.NumberFormat(locale, { style: "currency", currency: SECONDARY, maximumFractionDigits: 0 });

  function currencySymbol(loc, ccy) {
    try {
      var parts = new Intl.NumberFormat(loc, { style: "currency", currency: ccy }).formatToParts(0);
      for (var i = 0; i < parts.length; i++) if (parts[i].type === "currency") return parts[i].value;
    } catch (e) {}
    return ccy;
  }
  var SEC_SYMBOL = currencySymbol(locale, SECONDARY);

  // Label + wire the secondary toggle button, and reveal the toggle.
  var secBtn = document.querySelector("[data-fx-secondary]");
  if (secBtn) {
    secBtn.setAttribute("data-cur", SECONDARY);
    secBtn.textContent = SEC_SYMBOL;
  }
  var toggle = document.querySelector("[data-fx-toggle]");
  if (toggle) toggle.removeAttribute("hidden");

  // Initial currency: URL ?cur= (for previews) → saved choice → USD.
  var params = new URLSearchParams(window.location.search);
  var cur = (params.get("cur") || localStorage.getItem("fx-cur") || "USD").toUpperCase();
  if (cur !== SECONDARY) cur = "USD";

  function format(usd, short) {
    if (cur === SECONDARY) return (short ? fmtSEC0 : fmtSEC).format(usd * rate);
    return (short ? fmtUSD0 : fmtUSD).format(usd);
  }

  function render() {
    var els = document.querySelectorAll("[data-usd]");
    for (var i = 0; i < els.length; i++) {
      var raw = parseFloat(els[i].getAttribute("data-usd"));
      if (isNaN(raw)) continue;
      // data-usd-short → no cents (vitals strip, hero, big numbers).
      var short = els[i].hasAttribute("data-usd-short");
      var neg = raw < 0;
      var s = format(Math.abs(raw), short);
      // Preserve an explicit sign so "-$5" / "-<sym>5" read naturally.
      els[i].textContent = neg ? "-" + s : s;
    }
    // Toggle button active state.
    var btns = document.querySelectorAll("[data-cur]");
    for (var j = 0; j < btns.length; j++) {
      btns[j].classList.toggle("is-active", btns[j].getAttribute("data-cur") === cur);
      btns[j].setAttribute("aria-pressed", btns[j].getAttribute("data-cur") === cur ? "true" : "false");
    }
    // Rate readout.
    var ind = document.querySelector("[data-fx-rate]");
    if (ind) {
      var stamp = isLive ? "live" : (rateDate ? "as of " + String(rateDate).slice(0, 10) : "as of build");
      ind.textContent = "US$ 1 = " + SEC_SYMBOL + " " + rate.toFixed(3) + " · " + stamp;
    }

    // PPP orientation notes — only meaningful in the secondary view, and only if a
    // PPP factor was configured. On figures tagged data-ppp (goal / monthly budget).
    var sec = cur === SECONDARY;
    var notes = document.querySelectorAll("[data-ppp]");
    for (var k = 0; k < notes.length; k++) {
      var note = notes[k];
      var show = sec && ppp;
      note.style.display = show ? "" : "none";
      if (!show) continue;
      var stretch = rate / ppp; // how much further US$ goes in the secondary economy
      var pppUsd = parseFloat(note.getAttribute("data-ppp-usd"));
      var valEl = note.querySelector("[data-ppp-value]");
      if (valEl && !isNaN(pppUsd)) {
        valEl.textContent = fmtUSD0.format(Math.abs(pppUsd) * stretch);
      }
    }
  }

  function setCurrency(c) {
    cur = (c === SECONDARY) ? SECONDARY : "USD";
    try { localStorage.setItem("fx-cur", cur); } catch (e) {}
    render();
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-cur]") : null;
    if (btn) { e.preventDefault(); setCurrency(btn.getAttribute("data-cur")); }
  });

  render();

  // Live refresh — non-blocking; render again if a source answers.
  (function refresh() {
    var i = 0;
    function tryNext() {
      if (i >= SOURCES.length) return;
      var s = SOURCES[i++];
      fetch(s.url, { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (d) {
          var v = s.pick(d);
          if (v && isFinite(v)) { rate = v; rateDate = s.when(d) || rateDate; isLive = true; render(); }
          else tryNext();
        })
        .catch(tryNext);
    }
    tryNext();
  })();

  window.FX = { setCurrency: setCurrency, get rate() { return rate; }, get currency() { return cur; } };
})();
