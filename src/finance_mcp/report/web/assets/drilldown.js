/* drilldown.js — make every aggregate drillable to the atomic transaction.
 *
 * Discoverability (IDEO): each drillable figure announces itself — a count
 * affordance ("12 ›"), a pointer cursor, a hover lift, and a one-time hint —
 * so the user discovers they can open it. Click reveals the exact transactions
 * behind the number (date · merchant · amount), with a running total.
 *
 * Transactions are embedded as window.__TXNS = [{d,m,a,c}] (date, merchant,
 * amount, category) — served only behind the private auth wall.
 */
(function () {
  "use strict";
  var TXNS = window.__TXNS || [];
  if (!TXNS.length || !document.querySelectorAll) return;

  var fmtUSD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
  function money(a) { return (a < 0 ? "-" : "") + fmtUSD.format(Math.abs(a)); }
  function esc(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  function matches(key) {
    var i = key.indexOf(":");
    var type = key.slice(0, i), val = key.slice(i + 1), v = val.toLowerCase();
    return TXNS.filter(function (t) {
      if (type === "cat") return (t.c || "").toLowerCase() === v;
      if (type === "month") return (t.d || "").indexOf(val) === 0;
      if (type === "merchant") return (t.m || "").toLowerCase().indexOf(v) >= 0;
      return false;
    });
  }

  function panelHTML(txns) {
    var rows = txns.slice().sort(function (a, b) { return a.d < b.d ? 1 : (a.d > b.d ? -1 : 0); });
    var total = rows.reduce(function (s, t) { return s + t.a; }, 0);
    var h = '<div class="drill-panel"><div class="drill-head">'
          + rows.length + ' transaction' + (rows.length === 1 ? '' : 's')
          + ' &middot; ' + money(total) + '</div>';
    rows.forEach(function (t) {
      h += '<div class="drill-row"><span class="drill-date">' + esc((t.d || "").slice(5))
         + '</span><span class="drill-merch">' + esc(t.m)
         + '</span><span class="drill-amt' + (t.a < 0 ? '' : ' pos') + '">' + money(t.a) + '</span></div>';
    });
    return h + '</div>';
  }

  function closeAll(except) {
    document.querySelectorAll(".drill-panel").forEach(function (p) { p.remove(); });
    document.querySelectorAll(".drill-open").forEach(function (e) {
      if (e !== except) e.classList.remove("drill-open");
    });
  }

  document.querySelectorAll("[data-drill]").forEach(function (el) {
    var txns = matches(el.getAttribute("data-drill"));
    if (!txns.length) return;
    el.classList.add("drillable");
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    el.setAttribute("aria-expanded", "false");

    var hint = document.createElement("span");
    hint.className = "drill-hint";
    hint.innerHTML = txns.length + ' &rsaquo;';
    el.appendChild(hint);

    function toggle() {
      var next = el.nextElementSibling;
      var isOpen = next && next.classList && next.classList.contains("drill-panel");
      closeAll(el);
      if (isOpen) { el.classList.remove("drill-open"); el.setAttribute("aria-expanded", "false"); return; }
      el.insertAdjacentHTML("afterend", panelHTML(txns));
      el.classList.add("drill-open");
      el.setAttribute("aria-expanded", "true");
    }
    el.addEventListener("click", toggle);
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  });

  // One-time discoverability hint above the first drillable region.
  var first = document.querySelector(".drillable");
  if (first) {
    var region = first.closest(".section-body") || first.parentNode;
    var hint = document.createElement("div");
    hint.className = "drill-tip";
    hint.innerHTML = '&#9758; Tap any category or month to see the transactions behind it.';
    region.parentNode.insertBefore(hint, region);
  }
})();
