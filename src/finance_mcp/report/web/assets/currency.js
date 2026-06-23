/* currency.js — USD↔BRL toggle for finance.mcp reports.
 *
 * No backend. Every money figure is rendered server-side in USD with a
 * data-usd="<numeric>" attribute. This module:
 *   1. Reads a build-time fallback rate from window.__FX (baked by the Python
 *      pipeline at report generation).
 *   2. On load, refreshes the rate live from a free, no-key, CORS-enabled FX
 *      API (open.er-api.com, then frankfurter.dev), falling back to the baked
 *      rate if both are unreachable.
 *   3. Converts every [data-usd] element on toggle, persisting the choice.
 *
 * Deterministic math stays in Python; this only re-denominates display values.
 */
(function () {
  "use strict";

  var baked = window.__FX || {};
  var rate = typeof baked.rate === "number" ? baked.rate : 5.07;
  var rateDate = baked.date || null;
  var isLive = false;

  // World Bank PPP conversion factor for the secondary region (LCU per international $).
  // Updates annually; baked as a stable constant. ~2.5 means R$ 2.5 buys what
  // US$ 1 buys in the US, so US$ spent in the secondary region stretches ≈ rate/ppp further.
  var ppp = typeof baked.ppp === "number" ? baked.ppp : 2.5;

  // Live sources, tried in order. Each returns USD->BRL.
  var SOURCES = [
    { url: "https://open.er-api.com/v6/latest/USD",
      pick: function (d) { return d && d.rates && d.rates.BRL; },
      when: function (d) { return d && d.time_last_update_utc; } },
    { url: "https://api.frankfurter.dev/v1/latest?base=USD&symbols=BRL",
      pick: function (d) { return d && d.rates && d.rates.BRL; },
      when: function (d) { return d && d.date; } }
  ];

  var fmtUSD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  var fmtBRL = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
  // Whole-dollar formatter for the PPP "lives like ≈ $X" orientation figure.
  var fmtUSD0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

  // Initial currency: URL ?cur= (for previews) → saved choice → USD.
  var params = new URLSearchParams(window.location.search);
  var cur = (params.get("cur") || localStorage.getItem("fx-cur") || "USD").toUpperCase();
  if (cur !== "BRL") cur = "USD";

  var fmtBRL0 = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL", maximumFractionDigits: 0 });

  function format(usd, short) {
    if (cur === "BRL") return (short ? fmtBRL0 : fmtBRL).format(usd * rate);
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
      // Preserve an explicit sign so "-$5" / "-R$ 5" read naturally.
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
      ind.textContent = "US$ 1 = R$ " + rate.toFixed(3) + " · " + stamp;
    }

    // PPP orientation notes — only meaningful in BRL view, only on figures
    // tagged data-ppp (goal / monthly budget). Hidden entirely in USD view.
    var brl = cur === "BRL";
    var stretch = rate / ppp; // how much further US$ goes inside the secondary region
    var notes = document.querySelectorAll("[data-ppp]");
    for (var k = 0; k < notes.length; k++) {
      var note = notes[k];
      note.style.display = brl ? "" : "none";
      if (!brl) continue;
      var pppUsd = parseFloat(note.getAttribute("data-ppp-usd"));
      var valEl = note.querySelector("[data-ppp-value]");
      if (valEl && !isNaN(pppUsd)) {
        valEl.textContent = fmtUSD0.format(Math.abs(pppUsd) * stretch);
      }
    }
  }

  function setCurrency(c) {
    cur = c === "BRL" ? "BRL" : "USD";
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
