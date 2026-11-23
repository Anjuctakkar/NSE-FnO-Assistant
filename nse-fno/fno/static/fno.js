/*
 * NSE F&O UI. One script, two hosts:
 *   - standalone (python -m fno serve): mounts into <div id="fno-root">
 *   - the dashboard: add  <script src="/fno.js" defer></script>  to index.html and it adds an "F&O" tab by itself.
 * No dependencies: charts are plain SVG. All dynamic text goes through esc() or textContent.
 *
 * The log of suggestions you keep lives in THIS browser (localStorage), like the dashboard's Evaluation list: the server
 * only replays the entries you send it and stores nothing about you.
 */
(function (root) {
  "use strict";

  /* ------------------------------------------------------------------ helpers (pure; unit-tested in node) */
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fin = v => v != null && isFinite(v);
  const inr = (v, d) => !fin(v) ? "—" : (v < 0 ? "−" : "") + "₹" + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: d || 0 });
  const sinr = (v, d) => !fin(v) ? "—" : `<span class="${v >= 0 ? "pos" : "neg"}">${v >= 0 ? "+" : "−"}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: d || 0 })}</span>`;
  const pct = (v, d) => !fin(v) ? "—" : (v * 100).toFixed(d || 0) + "%";
  const sgn = (v, d) => !fin(v) ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d == null ? 2 : d);
  const parseDay = iso => new Date(String(iso).slice(0, 10) + "T00:00:00");
  const fdate = iso => { const d = parseDay(iso); return isNaN(d) ? esc(iso) : d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }); };
  const sdate = iso => { const d = parseDay(iso); return isNaN(d) ? esc(iso) : d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" }); };
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

  /** Round tick values covering [lo, hi], about n of them. */
  function niceTicks(lo, hi, n) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / Math.max(1, n), mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag, out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : +v.toFixed(10));
    return out;
  }
  const compact = v => { const a = Math.abs(v), s = v < 0 ? "−" : ""; return a >= 1e7 ? s + (a / 1e7).toFixed(a >= 1e8 ? 0 : 1) + "Cr" : a >= 1e5 ? s + (a / 1e5).toFixed(a >= 1e6 ? 0 : 1) + "L" : a >= 1e3 ? s + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k" : s + Math.round(a); };

  /** What the position's Greeks mean in a sentence each, for someone who does not think in gamma. */
  function plainWords(s) {
    const g = s.greeks, be = (s.risk.breakevens || []), out = [];
    if (s.kind === "futures") return ["Moves one-for-one with the stock: " + (g.delta > 0 ? "gains if it rises, loses if it falls." : "gains if it falls, loses if it rises."), "No time decay and no volatility exposure, but no cap on the loss either: the stop is what limits it."];
    if (be.length >= 2) out.push(`Makes money if the stock stays between ${inr(be[0])} and ${inr(be[be.length - 1])} at expiry.`);
    else if (be.length === 1) out.push(g.delta >= 0 ? `Makes money if the stock is above ${inr(be[0])} at expiry.` : `Makes money if the stock is below ${inr(be[0])} at expiry.`);
    out.push(g.theta >= 0 ? `Time is on your side: roughly ${inr(Math.abs(g.theta))} a day of decay works in your favour.` : `Time is against you: roughly ${inr(Math.abs(g.theta))} a day is lost to decay if nothing moves.`);
    out.push(g.vega >= 0 ? "Gains if volatility rises, loses if it falls." : "Gains if volatility falls, loses if it rises (for example after a big move).");
    return out;
  }

  /** Client-side filter chips for the scan list. bias: all|bullish|bearish|neutral, kind: all|credit|debit|futures. */
  function filterIdeas(ideas, f) {
    return ideas.filter(i => (f.bias === "all" || (i.scan && i.scan.bias) === f.bias) && (f.kind === "all" || i.kind === f.kind));
  }
  const countWhere = (ideas, key, val) => ideas.filter(i => key === "bias" ? (i.scan && i.scan.bias) === val : i.kind === val).length;
  const strikeTxt = k => Number(k).toLocaleString("en-IN", { maximumFractionDigits: 2 });

  const STATUS = { target: ["Target hit", "good"], stop: ["Stopped", "bad"], invalidation: ["Invalidated", "bad"], time: ["Time exit", "warn"], expiry: ["Expired", "warn"], open: ["Open", "info"], no_data: ["No data", "mute"] };
  const PARTS = [["signal", "Signal fit", 25], ["iv_fit", "IV fit", 25], ["variance_premium", "Variance premium", 15], ["greek_fit", "Greek fit", 20], ["quality", "Trade quality", 15]];
  const PENALTIES = [["cost_penalty", "Costs"], ["extension_penalty", "Chasing a stretched move"]];

  /* ------------------------------------------------------------------ SVG charts */
  const CHARTS = new Map();
  let chartSeq = 0;

  /**
   * opts: xs, series [{name, ys, color, dash, width}], height, xfmt, yfmt, zero (draw a zero line), fill (series index
   * shaded green above / red below zero), marks [{x, label, color}]. Returns an HTML string; call hookCharts(container).
   */
  function lineChart(o) {
    const id = "c" + (++chartSeq), W = 640, H = o.height || 280, m = { l: 58, r: 14, t: 14, b: 26 };
    const xs = o.xs, all = [];
    o.series.forEach(s => s.ys.forEach(v => { if (fin(v)) all.push(v); }));
    if (o.zero) all.push(0);
    let lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (!(hi > lo)) { hi += 1; lo -= 1; }
    const padY = (hi - lo) * 0.07; lo -= padY; hi += padY;
    const x0 = xs[0], x1 = xs[xs.length - 1], iw = W - m.l - m.r, ih = H - m.t - m.b;
    const X = v => m.l + (v - x0) / ((x1 - x0) || 1) * iw, Y = v => m.t + (hi - v) / (hi - lo) * ih;
    const yf = o.yfmt || compact, xf = o.xfmt || (v => String(Math.round(v)));
    let g = "";
    niceTicks(lo, hi, 5).forEach(t => { g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"/><text class="tick" x="${m.l - 6}" y="${(Y(t) + 3.5).toFixed(1)}" text-anchor="end">${esc(yf(t))}</text>`; });
    niceTicks(x0, x1, 6).forEach(t => { g += `<text class="tick" x="${X(t).toFixed(1)}" y="${H - 8}" text-anchor="middle">${esc(xf(t))}</text>`; });
    if (o.zero) g += `<line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}"/>`;
    if (o.fill != null) {
      const ys = o.series[o.fill].ys, z = Y(0).toFixed(1);
      const poly = xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" L");
      const area = `M${X(x0).toFixed(1)},${z} L${poly} L${X(x1).toFixed(1)},${z} Z`;
      g += `<clipPath id="${id}u"><rect x="${m.l}" y="${m.t}" width="${iw}" height="${Math.max(0, Y(0) - m.t).toFixed(1)}"/></clipPath>` +
           `<clipPath id="${id}d"><rect x="${m.l}" y="${Y(0).toFixed(1)}" width="${iw}" height="${Math.max(0, H - m.b - Y(0)).toFixed(1)}"/></clipPath>` +
           `<path d="${area}" class="area-up" clip-path="url(#${id}u)"/><path d="${area}" class="area-dn" clip-path="url(#${id}d)"/>`;
    }
    (o.marks || []).forEach(k => {
      if (k.x < x0 || k.x > x1) return;
      g += `<line class="mark" x1="${X(k.x).toFixed(1)}" x2="${X(k.x).toFixed(1)}" y1="${m.t}" y2="${H - m.b}" stroke="${k.color || "#52675a"}"/>` +
           `<text class="marklbl" x="${(X(k.x) + 3).toFixed(1)}" y="${m.t + 9 + (k.row || 0) * 11}" fill="${k.color || "#52675a"}">${esc(k.label)}</text>`;
    });
    o.series.forEach(s => {
      const pts = xs.map((x, i) => fin(s.ys[i]) ? `${X(x).toFixed(1)},${Y(s.ys[i]).toFixed(1)}` : null).filter(Boolean);
      g += `<path d="M${pts.join(" L")}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round"/>`;
    });
    g += `<g class="hv" style="display:none"><line class="hvl" y1="${m.t}" y2="${H - m.b}"/>${o.series.map(s => `<circle r="3.5" fill="${s.color}"/>`).join("")}</g><rect class="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/>`;
    CHARTS.set(id, { xs, series: o.series, W, m, iw, x0, x1, X, Y, xf: o.xtip || xf, yf: o.ytip || (v => inr(v)), lbl: o.lbl || "" });
    const legend = o.series.length > 1 ? `<div class="legend">${o.series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
    return `<div class="chart" data-chart="${id}"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(o.aria || "chart")}">${g}</svg><div class="tip" style="display:none"></div>${legend}</div>`;
  }

  function hookCharts(el) {
    el.querySelectorAll(".chart[data-chart]").forEach(box => {
      const c = CHARTS.get(box.dataset.chart), svg = box.querySelector("svg"), hv = svg.querySelector(".hv"), tip = box.querySelector(".tip");
      if (!c) return;
      const move = ev => {
        const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) / r.width * c.W;
        const xv = c.x0 + clamp((px - c.m.l) / c.iw, 0, 1) * (c.x1 - c.x0);
        let best = 0, bd = Infinity;
        c.xs.forEach((x, i) => { const d = Math.abs(x - xv); if (d < bd) { bd = d; best = i; } });
        const sx = c.X(c.xs[best]);
        hv.style.display = "";
        hv.querySelector(".hvl").setAttribute("x1", sx); hv.querySelector(".hvl").setAttribute("x2", sx);
        hv.querySelectorAll("circle").forEach((ci, k) => { const y = c.series[k].ys[best]; ci.setAttribute("cx", sx); ci.setAttribute("cy", fin(y) ? c.Y(y) : -10); });
        tip.style.display = "";
        tip.innerHTML = `<b>${esc(c.lbl)}${esc(c.xf(c.xs[best]))}</b>` + c.series.map(s => `<div><i style="background:${s.color}"></i>${esc(s.name)} <b>${fin(s.ys[best]) ? esc(c.yf(s.ys[best])) : "—"}</b></div>`).join("");
        const left = sx / c.W * 100;
        tip.style.left = left > 60 ? "auto" : (left + 2) + "%"; tip.style.right = left > 60 ? (100 - left + 2) + "%" : "auto";
      };
      svg.addEventListener("mousemove", move);
      svg.addEventListener("mouseleave", () => { hv.style.display = "none"; tip.style.display = "none"; });
    });
  }

  /* ------------------------------------------------------------------ styles */
  const CSS = `
.fno{--f-bg:#f1f7ef;--f-side:#e7f0e5;--f-panel:#fbfdfb;--f-field:#fff;--f-border:#b7c9b8;--f-text:#17251b;--f-muted:#4f6555;--f-good:#176b44;--f-info:#2f704b;--f-bad:#b42332;--f-warn:#825600;color:var(--f-text);background:var(--f-bg);min-height:100vh;font:400 14px/1.5 "Segoe UI",Inter,-apple-system,BlinkMacSystemFont,Roboto,sans-serif;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.fno *{box-sizing:border-box}
.fno-layout{display:grid;grid-template-columns:320px minmax(0,1fr);min-height:calc(100vh - 52px)}
.fno-side{background:var(--f-side);border-right:1px solid var(--f-border);padding:4px 0 28px;position:sticky;top:52px;align-self:start;height:calc(100vh - 52px);overflow-y:auto}
.fno-sec{padding:16px 20px;border-bottom:1px solid var(--f-border)}
.fno-sec:last-child{border-bottom:none}
.fno h2{font-size:14px;font-weight:700;letter-spacing:-.01em;margin:0 0 10px;color:var(--f-text)}
.fno h3{font-size:13.5px;font-weight:700;margin:0 0 8px;color:var(--f-muted)}
.fno label{display:block;font-size:12.5px;font-weight:600;color:var(--f-muted);margin:12px 0 4px}
.fno label.first{margin-top:0}
.fno select,.fno input[type=text],.fno input[type=password],.fno input[type=number]{width:100%;background:var(--f-field);border:1px solid #9db59f;color:var(--f-text);border-radius:7px;padding:8px 10px;font-size:13px;font-family:inherit;box-shadow:0 1px 2px rgba(27,58,35,.06)}
.fno select:focus,.fno input:focus{border-color:var(--f-info);outline:3px solid rgba(47,112,75,.18);outline-offset:1px}
.fno button:focus-visible,.fno [role=tab]:focus-visible{outline:3px solid rgba(47,112,75,.3);outline-offset:2px}
.fno .row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fno button{width:auto;font-family:inherit;font-size:13px;font-weight:600;border-radius:7px;cursor:pointer;padding:8px 14px;border:1px solid #91a995;background:#f9fcf8;color:var(--f-text);box-shadow:0 1px 2px rgba(27,58,35,.08);transition:background .15s ease,border-color .15s ease,box-shadow .15s ease,transform .15s ease}
.fno button:hover{border-color:var(--f-info);background:#edf5eb;box-shadow:0 2px 5px rgba(27,58,35,.12)}
.fno button:active{transform:translateY(1px)}
.fno button:disabled{opacity:.5;cursor:not-allowed}
.fno .primary{width:100%;background:#286b46;border-color:#1e5b39;color:#fff;font-weight:700;margin-top:4px;padding:10px;border-radius:8px;box-shadow:0 2px 5px rgba(28,78,48,.2)}
.fno .primary:hover{background:#1e5b39;border-color:#174a2e;filter:none}
.fno .ghost{width:100%;margin-top:8px}
.fno .small{padding:4px 10px;font-size:12px}
.fno .btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.fno .hint,.fno .fine{font-size:12.5px;color:var(--f-muted);line-height:1.5;margin:6px 0 0}
.fno .fine{font-size:11.5px;margin-top:14px}
.fno .status{font-size:12px;margin-top:10px;line-height:1.45;color:var(--f-muted)}
.fno .status.err,.fno .err{color:var(--f-bad)}
.fno .check{display:flex;align-items:center;gap:8px;margin:12px 0 0;color:var(--f-text);font-size:13px}
.fno .check input{width:auto;margin:0}
.fno-main{padding:20px 28px 60px;min-width:0}
.fno-toolbar{display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--f-border);margin-bottom:18px}
.fno-tabs{display:flex;flex-wrap:wrap;gap:2px;flex:1;min-width:0}
.fno-app-version{flex:none;font-size:12px}
.fno-tabs button{border:1px solid transparent;border-bottom:2px solid transparent;border-radius:7px 7px 0 0;padding:9px 16px;color:var(--f-muted);font-size:13.5px;background:transparent;box-shadow:none}
.fno-tabs button:hover{background:#e8f2e6;border-color:transparent}
.fno-tabs button.on{color:var(--f-info);border-bottom-color:var(--f-info);font-weight:700}
.fno-tabs .badge{margin-left:6px}
.fno .pos{color:var(--f-good)} .fno .neg{color:var(--f-bad)} .fno .warn{color:var(--f-warn)} .fno .mute{color:var(--f-muted)}
.fno .panel{background:var(--f-panel);border:1px solid var(--f-border);border-radius:9px;padding:16px;margin-bottom:18px;box-shadow:0 2px 8px rgba(27,58,35,.06)}
.fno .empty{color:var(--f-muted);text-align:center;padding:48px 20px;line-height:1.7}
.fno .note{border:1px solid var(--f-border);border-left:3px solid var(--f-warn);background:rgba(130,86,0,.07);border-radius:7px;padding:9px 12px;font-size:12.5px;line-height:1.5;margin:0 0 10px;color:var(--f-text)}
.fno .note.info{border-left-color:var(--f-info);background:rgba(47,112,75,.07)}
.fno .note.bad{border-left-color:var(--f-bad);background:rgba(180,35,50,.07)}
.fno .badge{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 8px;border:1px solid #d2e0d1;border-radius:5px;background:#edf4eb;color:var(--f-muted);white-space:nowrap}
.fno .badge.good{background:rgba(23,107,68,.12);color:var(--f-good)} .fno .badge.bad{background:rgba(180,35,50,.12);color:var(--f-bad)}
.fno .badge.warn{background:rgba(130,86,0,.12);color:var(--f-warn)} .fno .badge.info{background:rgba(47,112,75,.12);color:var(--f-info)}
.fno .badge.buy{background:rgba(23,107,68,.12);color:var(--f-good)} .fno .badge.sell{background:rgba(180,35,50,.12);color:var(--f-bad)}
.fno .strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:18px}
.fno .tile{background:var(--f-panel);border:1px solid var(--f-border);border-radius:9px;padding:12px 14px;box-shadow:0 2px 7px rgba(27,58,35,.05)}
.fno .tile .l{font-size:12px;color:var(--f-muted);margin-bottom:4px;display:flex;justify-content:space-between;gap:6px}
.fno .tile .v{font-size:18px;font-weight:650} .fno .tile .s{font-size:12px;color:var(--f-muted);margin-top:3px;line-height:1.4}
.fno .track{position:relative;height:6px;border-radius:3px;margin:12px 0 6px;background:linear-gradient(90deg,var(--f-bad),var(--f-warn),var(--f-good));opacity:.85}
.fno .track.iv{background:linear-gradient(90deg,var(--f-info),var(--f-warn),var(--f-bad))}
.fno .track .dot{position:absolute;top:-4px;width:14px;height:14px;margin-left:-7px;border-radius:50%;background:var(--f-text);border:2px solid var(--f-bg)}
.fno .track .mid{position:absolute;left:50%;top:-3px;width:1px;height:12px;background:var(--f-bg)}
.fno .sug{background:var(--f-panel);border:1px solid var(--f-border);border-radius:9px;margin-bottom:20px;overflow:hidden;box-shadow:0 2px 8px rgba(27,58,35,.06)}
.fno .sug-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--f-border)}
.fno .sug-head .rank{color:var(--f-muted);font-size:13px}
.fno .sug-head .name{font-size:17px;font-weight:650}
.fno .sug-head .meta{color:var(--f-muted);font-size:12.5px;flex:1;min-width:200px}
.fno .score{display:flex;align-items:center;gap:8px;font-weight:650}
.fno .score .bar{width:90px;height:8px;border-radius:4px;background:var(--f-border);overflow:hidden}
.fno .score .bar i{display:block;height:100%;background:var(--f-good)}
.fno .sug-body{display:grid;grid-template-columns:minmax(300px,420px) minmax(0,1fr);gap:18px;padding:16px 18px}
.fno table{border-collapse:collapse;width:100%;font-size:13px}
.fno th,.fno td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--f-border);white-space:nowrap}
.fno th{color:var(--f-muted);font-weight:600;font-size:12px}
.fno td.r,.fno th.r{text-align:right}
.fno table.compact{font-size:12.5px} .fno table.compact th,.fno table.compact td{padding:5px 8px}
.fno .scroll{overflow:auto;max-height:60vh;border:1px solid var(--f-border);border-radius:6px}
.fno .grid4{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px}
.fno .mini{border:1px solid var(--f-border);border-radius:6px;padding:8px 10px}
.fno .mini .l{font-size:11.5px;color:var(--f-muted)} .fno .mini .v{font-size:15px;font-weight:650;margin-top:1px} .fno .mini .s{font-size:11.5px;color:var(--f-muted);margin-top:1px}
.fno .sec-title{font-size:12px;color:var(--f-muted);font-weight:650;text-transform:uppercase;letter-spacing:.04em;margin:16px 0 6px}
.fno .sug-foot{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;padding:0 18px 16px}
.fno .parts{display:grid;gap:7px}
.fno .part{display:grid;grid-template-columns:140px 1fr 44px;align-items:center;gap:8px;font-size:12.5px}
.fno .part .t{height:7px;background:var(--f-border);border-radius:4px;overflow:hidden} .fno .part .t i{display:block;height:100%;background:var(--f-info)}
.fno .part.neg .t i{background:var(--f-bad)} .fno .part span:last-child{text-align:right}
.fno ul.why{margin:0;padding-left:18px;font-size:12.5px;line-height:1.6;color:var(--f-muted)} .fno ul.why li{margin-bottom:3px}
.fno .plan{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:12.5px} .fno .plan dt{color:var(--f-muted)} .fno .plan dd{margin:0}
.fno .actions{display:flex;gap:8px;flex-wrap:wrap;padding:0 18px 16px}
.fno .verdict{padding:14px 18px;border-bottom:1px solid var(--f-border);background:rgba(47,112,75,.05)}
.fno .verdict .big{font-size:18px;line-height:1.35}.fno .verdict .big b{font-weight:700}
.fno .verdict .sub{font-size:12.5px;color:var(--f-muted);margin-top:4px;line-height:1.5}.fno .verdict .sub b{color:var(--f-text)}
.fno .chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.fno .chip{border:1px solid var(--f-border);border-radius:6px;padding:9px 12px;display:flex;flex-direction:column;gap:2px}
.fno .chip span{font-size:12px;color:var(--f-muted)}.fno .chip b{font-size:15px}
.fno .chip.good{border-color:rgba(23,107,68,.4)}.fno .chip.bad{border-color:rgba(180,35,50,.4)}
.fno ul.plain{color:var(--f-text);font-size:13px}
.fno details.adv{margin:10px 0 0;padding:4px 10px;background:transparent}
.fno .steps{display:grid;gap:14px;max-width:640px;margin:40px auto}
.fno .step{display:flex;gap:14px;background:var(--f-panel);border:1px solid var(--f-border);border-radius:8px;padding:14px 16px;line-height:1.55;color:var(--f-muted)}
.fno .step b{flex:none;width:28px;height:28px;border-radius:50%;background:#286b46;color:#fff;display:flex;align-items:center;justify-content:center}
.fno .step h3{margin:0 0 3px;color:var(--f-text);font-size:14px}.fno .step i{color:var(--f-text);font-style:normal;font-weight:600}
.fno .issue{border-left:3px solid var(--f-warn)}
.fno .meter{height:8px;border-radius:4px;background:var(--f-border);overflow:hidden;max-width:420px;margin-top:4px}.fno .meter i{display:block;height:100%;background:var(--f-warn)}
.fno .seg{display:grid;grid-template-columns:1fr 1fr;border:1px solid #91a995;border-radius:7px;overflow:hidden;background:#f9fcf8}
.fno .seg button{border:none;border-radius:0;padding:8px 6px;color:var(--f-muted);background:transparent}
.fno .seg button.on{background:#286b46;color:#fff;font-weight:700}
.fno .fchips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px}
.fno .fchip{padding:4px 11px;border-radius:999px;font-size:12.5px;color:var(--f-muted)}
.fno .fchip.on{color:var(--f-text);border-color:var(--f-info);background:rgba(47,112,75,.12)}
.fno .fchip small{opacity:.7;margin-left:2px}
.fno .fchips .sep{width:1px;height:18px;background:var(--f-border);margin:0 6px}
.fno .sug-head .sym{font-size:18px;font-weight:700}
.fno .oichips{display:flex;flex-wrap:wrap;gap:6px}
.fno .oichip{font-size:11.5px;padding:2px 8px;border-radius:4px;border:1px solid var(--f-border);color:var(--f-muted)}
.fno .oichip.up{color:var(--f-good);border-color:rgba(23,107,68,.5)}.fno .oichip.down{color:var(--f-bad);border-color:rgba(180,35,50,.5)}
.fno .mix{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--f-border);margin:12px 0 7px}
.fno .mix i{display:block;height:100%}.fno .mix .b{background:var(--f-good)}.fno .mix .n{background:var(--f-muted);opacity:.5}.fno .mix .r{background:var(--f-bad)}
.fno .chart{position:relative}
.fno .chart svg{width:100%;height:auto;display:block}
.fno .chart .grid{stroke:var(--f-border);stroke-width:1} .fno .chart .zero{stroke:var(--f-muted);stroke-width:1.2}
.fno .chart .tick{fill:var(--f-muted);font-size:10.5px} .fno .chart .marklbl{font-size:10px}
.fno .chart .mark{stroke-width:1;stroke-dasharray:3 3;opacity:.85}
.fno .chart .area-up{fill:rgba(23,107,68,.16)} .fno .chart .area-dn{fill:rgba(180,35,50,.14)}
.fno .chart .hvl{stroke:var(--f-muted);stroke-width:1}
.fno .chart .tip{position:absolute;top:8px;background:#fbfdfb;border:1px solid var(--f-border);border-radius:6px;padding:6px 9px;font-size:12px;pointer-events:none;line-height:1.5;min-width:130px;box-shadow:0 4px 12px rgba(27,58,35,.12)}
.fno .chart .tip i,.fno .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}
.fno .legend{display:flex;gap:16px;font-size:12px;color:var(--f-muted);margin:4px 0 0 58px}
.fno .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.fno .attr{display:grid;gap:6px;font-size:12.5px}
.fno .attr .a{display:grid;grid-template-columns:130px 1fr 90px;gap:8px;align-items:center}
.fno .attr .t{position:relative;height:10px;background:var(--f-field);border-radius:3px}
.fno .attr .t i{position:absolute;top:0;height:100%;border-radius:3px}
.fno .attr .t b{position:absolute;left:50%;top:-2px;width:1px;height:14px;background:var(--f-muted)}
.fno details{border:1px solid var(--f-border);border-radius:6px;padding:6px 12px;margin-bottom:10px;background:var(--f-panel)}
.fno details summary{cursor:pointer;font-size:13px;color:var(--f-muted);padding:3px 0}
.fno details[open] summary{margin-bottom:8px}
.fno .busy{display:inline-block;width:12px;height:12px;border:2px solid var(--f-border);border-top-color:var(--f-info);border-radius:50%;animation:fnospin .8s linear infinite;vertical-align:-2px;margin-right:6px}
.fno .fno-chat-toggle{position:fixed;right:22px;bottom:22px;z-index:1000;border:0;border-radius:999px;background:var(--f-info);color:#fff;box-shadow:0 4px 18px rgba(0,0,0,.35);display:flex;align-items:center;gap:8px;font-weight:650;padding:11px 16px}
.fno .fno-chat-toggle:hover{filter:brightness(1.08)}
.fno .fno-chat-toggle svg{width:18px;height:18px}
.fno .fno-chat-panel{position:fixed;right:22px;bottom:78px;z-index:1001;width:min(390px,calc(100vw - 28px));height:min(640px,calc(100vh - 110px));display:flex;flex-direction:column;background:var(--f-bg);border:1px solid var(--f-border);border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,.45);padding:14px}
.fno .fno-chat-panel[hidden]{display:none}
.fno-chat-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.fno-chat-head h2{margin:0;font-size:15px}
.fno-chat-mode{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:6px 0;font-size:12px;color:var(--f-muted)}
.fno-chat-history{flex:1;min-height:80px;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:12px 0}
.fno-chat-msg{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.45;font-size:13px;border:1px solid var(--f-border);background:var(--f-panel);border-radius:8px;padding:8px 10px}
.fno-chat-msg.user{align-self:flex-end;max-width:90%;background:rgba(47,112,75,.1)}
.fno-chat-msg.assistant{align-self:stretch}
.fno-chat-panel label{margin:8px 0 4px}
.fno-chat-panel select,.fno-chat-panel input,.fno-chat-panel textarea{width:100%;background:var(--f-field);border:1px solid var(--f-border);color:var(--f-text);border-radius:6px;padding:7px 9px;font:inherit;font-size:12.5px}
.fno-chat-panel textarea{resize:vertical;min-height:56px;max-height:120px}
.fno-chat-note{font-size:11px;color:var(--f-muted);line-height:1.4;margin:5px 0}
.fno-chat-form{display:grid;grid-template-columns:1fr auto;gap:7px;align-items:end}
.fno-chat-form button{background:var(--f-info);border-color:var(--f-info);color:#fff}
@keyframes fnospin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.fno .busy{animation:none}}
@media (max-width:1000px){.fno-layout{grid-template-columns:1fr}.fno-side{position:static;height:auto;border-right:none;border-bottom:1px solid var(--f-border)}.fno .sug-body{grid-template-columns:1fr}.fno-main{padding:16px}}
@media (max-width:520px){.fno .fno-chat-toggle{right:12px;bottom:12px}.fno .fno-chat-panel{right:8px;bottom:68px;width:calc(100vw - 16px);height:calc(100vh - 82px)}}
`;

  /* ------------------------------------------------------------------ state */
  const API = (root.FNO_API || "") + "/api/fno";
  const S = { mode: "market", view: "scan", scan: null, filter: { bias: "all", kind: "all" }, open: {}, source: "bhavcopy", status: null, symbols: [], tab: "sug", sug: null, bt: null, log: [], out: {}, outSummary: null, scope: "local", host: null, liveQuote: null, breezeConnection: "not tested", chat: [], chatMode: "app-only" };
  const $ = sel => S.host.querySelector(sel);
  const val = sel => { const e = $(sel); return e ? e.value : ""; };

  async function api(path, opts) {
    let res;
    try { res = await fetch(API + path, opts); } catch (e) { throw new Error("Couldn't reach the server."); }
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : res.status === 403 ? "You don't have permission for that." : "Something went wrong (" + res.status + ").");
    return d;
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  function say(sel, html, cls) { const e = $(sel); if (e) { e.className = "status" + (cls ? " " + cls : ""); e.innerHTML = html; } }
  const busy = text => `<span class="busy"></span>${esc(text)}`;

  /* ------------------------------------------------------------------ local log */
  const logKey = () => "fnoLog:v1:" + S.scope;
  function loadLog() { try { S.log = JSON.parse(localStorage.getItem(logKey()) || "[]"); } catch (e) { S.log = []; } if (!Array.isArray(S.log)) S.log = []; }
  function saveLog() { try { localStorage.setItem(logKey(), JSON.stringify(S.log)); return true; } catch (e) { return false; } }
  const sameKey = (a, b) => a.symbol === b.symbol && a.asof === b.asof && a.structure.name === b.structure.name;
  function slim(s) {                                     // what the server needs to replay it; drops the chart arrays
    const o = {}; ["id", "symbol", "asof", "structure", "exit_plan", "score", "reasons", "risk", "greeks", "pop", "regime", "dte", "warnings", "source", "kind", "spot"].forEach(k => { o[k] = s[k]; });
    o.logged_at = new Date().toISOString().slice(0, 19); o.taken = false; return o;
  }

  /* ------------------------------------------------------------------ shell */
  function shell() {
    return `<div class="fno"><div class="fno-layout">
<aside class="fno-side">
  <div class="fno-sec"><h2>Data</h2>
    <label class="first" for="fnoSource">Market data</label>
    <select id="fnoSource"><option value="bhavcopy">NSE F&amp;O bhavcopy (EOD)</option><option value="breeze-live">ICICI Breeze live (5-minute)</option><option value="synthetic">Synthetic demo (not real)</option></select>
    <div class="hint" id="fnoStatus">Loading…</div>
    <div class="hint" id="fnoDateInfo"></div>
    <div id="fnoDataReal"><label for="fnoHist">History to download</label>
      <select id="fnoHist"><option value="90">3 months (the minimum)</option><option value="180">6 months</option><option value="365" selected>1 year (recommended)</option></select>
      <div class="btns"><button id="fnoSyncLatest" class="small" type="button">Sync latest</button><button id="fnoSync" class="small" type="button">Sync history</button><button id="fnoStop" class="small" type="button" hidden>Stop</button><button id="fnoImport" class="small" type="button">Import file…</button></div>
      <div class="hint">NSE F&amp;O bhavcopy contains end-of-day settlement data, not live or intraday quotes. Sync checks for newly published files.</div></div>
    <input type="file" id="fnoFile" accept=".zip,.csv" hidden />
    <div class="status" id="fnoDataMsg"></div>
  </div>
  <div class="fno-sec"><h2>What to look at</h2>
    <div class="seg" role="group" aria-label="What to look at"><button type="button" id="fnoModeMkt" class="on" aria-pressed="true">Whole market</button><button type="button" id="fnoModeOne" aria-pressed="false">One stock</button></div>
    <div class="hint" id="fnoModeHint">Scans every F&amp;O stock and ranks the best trades by Greeks and open interest. No stock needed.</div>
    <div id="fnoSymWrap" style="display:none"><label for="fnoSym">NSE symbol</label>
      <input type="text" id="fnoSym" list="fnoSyms" placeholder="e.g. TCS" autocomplete="off" spellcheck="false" /><datalist id="fnoSyms"></datalist>
      <div class="btns"><button id="fnoLiveRefresh" class="small" type="button">Refresh live quote</button></div>
      <div class="hint" id="fnoLiveQuote">ICICI Breeze quote is fetched on demand. Configure credentials in Settings or use server environment credentials. In Breeze-live mode, strategy inputs use live data only.</div></div>
    <div id="fnoAsofWrap"><label for="fnoAsof">As of</label>
      <select id="fnoAsof"><option value="">Latest day</option></select></div>
    <div id="fnoExpiryWrap"><label for="fnoExpiry">Option expiry</label>
      <select id="fnoExpiry" disabled><option value="">Loading expiries…</option></select>
      <div class="hint" id="fnoExpiryHint">Only expiries with option data on the selected day are listed.</div></div>
  </div>
  <div class="fno-sec"><h2>Rules</h2>
    <label class="first" for="fnoRisk">Risk per trade (₹, sets the lots)</label>
    <input type="number" id="fnoRisk" value="100000" min="0" step="5000" />
    <div class="hint">The number of lots is set so the worst case loses no more than this. If one lot risks more, the trade is skipped.</div>
    <details class="adv"><summary>Advanced</summary>
      <div class="row2"><div><label for="fnoMin" class="first">Minimum score</label><input type="number" id="fnoMin" value="55" min="0" max="100" /></div>
        <div><label for="fnoVol" class="first">Min volume</label><input type="number" id="fnoVol" value="10" min="0" /></div></div>
      <div class="row2"><div><label for="fnoOi">Min open interest</label><input type="number" id="fnoOi" value="0" min="0" /></div>
        <div id="fnoIvWrap" style="display:none"><label for="fnoIv">IV scale</label><input type="number" id="fnoIv" value="1" min="0.5" max="2" step="0.05" /></div></div>
      <label class="check"><input type="checkbox" id="fnoFut" checked /> Allow futures</label>
      <div class="hint">Volume and open interest skip contracts that barely trade: their prices are stale.</div></details>
  </div>
  <div class="fno-sec">
    <button class="primary" id="fnoRunSug" type="button">Find trades</button>
    <button class="ghost" id="fnoRunBt" type="button">Run backtest</button>
    <div class="status" id="fnoMsg"></div>
    <p class="fine">Information only, not investment advice. Suggestions are the output of a fixed rule set. Futures and options can lose more than you expect; most individual F&amp;O traders lose money.</p>
  </div>
</aside>
<main class="fno-main">
  <div class="fno-toolbar"><div class="fno-tabs" role="tablist"><button data-t="sug" class="on" role="tab">Ideas</button><button data-t="bt" role="tab">Backtest</button><button data-t="log" role="tab">My log<span class="badge" id="fnoLogN">0</span></button><button data-t="settings" role="tab">Settings</button></div><span class="badge info fno-app-version" id="fnoAppVersion">App version loading…</span></div>
  <div id="fnoPanSug"></div><div id="fnoPanBt" hidden></div><div id="fnoPanLog" hidden></div>
  <div id="fnoPanSettings" hidden>
    <div class="panel" style="max-width:620px">
      <h2>ICICI Breeze connection</h2>
      <p class="hint">Enter your Breeze API key, API secret, and session token, then test the connection. Credentials are sent to this F&amp;O server for authentication and are not saved by this page. Leave all fields blank to use server environment credentials.</p>
      <label for="fnoBreezeApiKey">API key</label>
      <input type="text" id="fnoBreezeApiKey" autocomplete="off" spellcheck="false" />
      <label for="fnoBreezeApiSecret">API secret</label>
      <input type="password" id="fnoBreezeApiSecret" autocomplete="new-password" spellcheck="false" />
      <label for="fnoBreezeSessionToken">Session token</label>
      <input type="password" id="fnoBreezeSessionToken" autocomplete="new-password" spellcheck="false" />
      <div class="btns"><button id="fnoBreezeTest" type="button">Test connection</button></div>
      <div class="status" id="fnoBreezeTestStatus" role="status" aria-live="polite"></div>
      <p class="hint">For live quotes, the credentials entered here are used while this page remains open. The optional Breeze SDK must be installed on the server.</p>
    </div>
  </div>
  </main>
  <button class="fno-chat-toggle" id="fnoChatToggle" type="button" aria-label="Ask the F&amp;O assistant" aria-expanded="false">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H6l-3 2v-6a7.5 7.5 0 1 1 17-3.5Z"/><path d="M8 11h8M8 14h5"/></svg><span>Ask</span>
  </button>
  <section class="fno-chat-panel" id="fnoChatPanel" role="dialog" aria-label="F&amp;O assistant" hidden>
      <div class="fno-chat-head"><h2>F&amp;O assistant</h2><button class="small" id="fnoChatClose" type="button" aria-label="Close chat">Close</button></div>
      <p class="fno-chat-note">App-only mode uses data already shown here. Model mode sends your prompt, recent chat, and displayed-data summary through this server to the selected provider. The API key is sent transiently to this server and forwarded; it is not logged or stored. The field clears when you switch mode or provider.</p>
      <div class="fno-chat-mode"><label for="fnoChatMode">Assistant mode</label><select id="fnoChatMode"><option value="app-only">App data only · no model</option><option value="model">AI model</option></select></div>
      <div id="fnoChatModelOptions" hidden>
        <label for="fnoChatProvider">Provider</label>
        <select id="fnoChatProvider"><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="gemini">Google Gemini</option></select>
        <label for="fnoChatModelName">Model</label>
        <input id="fnoChatModelName" type="text" autocomplete="off" spellcheck="false" />
        <label for="fnoChatApiKey">Provider API key</label>
        <input id="fnoChatApiKey" type="password" autocomplete="off" spellcheck="false" />
      </div>
      <div class="fno-chat-history" id="fnoChatHistory" role="log" aria-live="polite"></div>
      <form class="fno-chat-form" id="fnoChatForm">
        <textarea id="fnoChatMessage" maxlength="1000" aria-label="Your question" placeholder="Ask about connection, values, or ideas…"></textarea>
        <button id="fnoChatSend" type="submit">Send</button>
      </form>
      <div class="status" id="fnoChatStatus" role="status" aria-live="polite"></div>
  </section>
</div></div>`;
  }

  /* ------------------------------------------------------------------ data + status */
  function renderStatus() {
    const b = S.status && S.status.bhavcopy, el = $("#fnoStatus");
    const appVersion = S.status && S.status.app_version;
    $("#fnoAppVersion").textContent = appVersion ? `App version ${appVersion}` : "App version unavailable";
    const real = S.source === "bhavcopy";
    const live = S.source === "breeze-live";
    const currentDate = S.status && S.status.current_date;
    const bhavcopyDate = S.status && S.status.ltp_date;
    $("#fnoDateInfo").textContent = live
      ? "Breeze-live uses the current cash quote and option chain with recent 5-minute history."
      : `Current date: ${currentDate ? fdate(currentDate) : "—"} · Latest bhavcopy settlement date: ${real ? (bhavcopyDate ? fdate(bhavcopyDate) : "not available") : "not applicable in synthetic mode"}`;
    $("#fnoDataReal").style.display = real ? "" : "none";
    $("#fnoIvWrap").style.display = S.source === "synthetic" ? "" : "none";
    $("#fnoAsofWrap").style.display = real ? "" : "none";
    $("#fnoModeMkt").disabled = live;
    S.host.querySelector('[data-t="bt"]').hidden = live;
    $("#fnoRunBt").style.display = live || S.mode !== "one" ? "none" : "";
    $("#fnoFut").disabled = live;
    if (live) { $("#fnoFut").checked = false; $("#fnoModeHint").textContent = "Breeze-live evaluates one stock using the current option chain and 5-minute data."; }
    if (live) { el.textContent = "Current Breeze quotes and recent 5-minute candles. No bhavcopy fallback."; }
    else if (!real) { el.textContent = "Generated stocks (DEMO, ALPHA, BRAVO, CHARLIE) with Black-Scholes option prices. For trying the screens only."; }
    else if (!b || !b.days) { el.innerHTML = `<span class="warn">No bhavcopy stored yet.</span> Pick how much history to download and press Sync, or Import a file you saved from nseindia.com.`; }
    else {
      const range = `${fdate(b.first).replace(/ \d{4}$/, "")} to ${fdate(b.last)} · ${b.symbols} stocks with options`;
      el.innerHTML = b.readiness === "too_short" ? `<span class="warn">Only ${b.days} days stored</span> (${esc(range)}).<br>Suggestions need at least ${b.min_days} trading days, and a year is better. Download more history below.`
        : b.readiness === "thin" ? `${b.days} days stored (${esc(range)}).<br><span class="warn">IV rank is more reliable with about a year.</span>`
        : `${b.days} trading days, ${esc(range)}.`;
    }
    const sel = $("#fnoAsof"), keep = sel.value;
    const dates = real && b ? b.dates.slice().reverse() : [];
    sel.innerHTML = `<option value="">Latest day</option>` + dates.map(d => `<option value="${esc(d)}">${esc(fdate(d))}</option>`).join("");
    if (dates.includes(keep)) sel.value = keep;
  }

  async function loadStatus() {
    try { S.status = await api("/status"); if (S.source === "bhavcopy" && !(S.status.bhavcopy && S.status.bhavcopy.days)) { S.source = "synthetic"; $("#fnoSource").value = "synthetic"; } }
    catch (e) { say("#fnoMsg", esc(e.message), "err"); }
    renderStatus(); await loadSymbols();
  }

  async function loadSymbols() {
    try { S.symbols = (await api("/symbols?source=" + encodeURIComponent(S.source))).symbols; } catch (e) { S.symbols = []; }
    $("#fnoSyms").innerHTML = S.symbols.map(s => `<option value="${esc(s)}"></option>`).join("");
    const inp = $("#fnoSym");
    if (!inp.value || (S.symbols.length && !S.symbols.includes(inp.value.toUpperCase()) && S.source === "synthetic")
        || (S.source === "breeze-live" && ["DEMO", "ALPHA", "BRAVO", "CHARLIE"].includes(inp.value.toUpperCase()))) {
      inp.value = S.symbols.includes("TCS") ? "TCS" : (S.source === "synthetic" ? (S.symbols[0] || "") : "");
    }
    $("#fnoVol").value = S.source === "bhavcopy" ? ($("#fnoVol").value || 10) : 0;
    await loadExpiryOptions();
  }

  async function refreshLiveQuote() {
    const symbol = val("#fnoSym").trim().toUpperCase(), button = $("#fnoLiveRefresh"), output = $("#fnoLiveQuote");
    if (!symbol) { output.textContent = "Enter an NSE symbol first."; return; }
    button.disabled = true;
    output.textContent = `Fetching the ICICI Breeze live quote for ${symbol}…`;
    try {
      const q = await post("/live/quote", Object.assign({ symbol }, breezeCredentials()));
      S.liveQuote = { symbol: q.symbol, ltp: q.ltp, change: q.change, change_pct: q.change_pct, timestamp: q.timestamp };
      const change = fin(q.change) ? ` · change ${sgn(q.change, 2)} (${sgn(q.change_pct, 2)}%)` : "";
      const range = ` · O ${inr(q.open, 2)} / H ${inr(q.high, 2)} / L ${inr(q.low, 2)}`;
      const strategySource = S.source === "breeze-live" ? "Strategy uses Breeze 5-minute data." : "Quote only; strategy inputs remain based on the selected source.";
      output.textContent = `${q.symbol} (${q.provider || "ICICI Breeze"}) · LTP ${inr(q.ltp, 2)}${change}${range} · Breeze timestamp ${q.timestamp || "unavailable"}. ${strategySource}`;
    } catch (e) {
      output.textContent = `Live quote unavailable: ${e.message}`;
    } finally { button.disabled = false; }
  }

  function breezeCredentials() {
    return {
      api_key: val("#fnoBreezeApiKey").trim(),
      api_secret: val("#fnoBreezeApiSecret").trim(),
      session_token: val("#fnoBreezeSessionToken").trim()
    };
  }

  async function testBreezeConnection() {
    const button = $("#fnoBreezeTest"), status = $("#fnoBreezeTestStatus");
    button.disabled = true;
    status.className = "status";
    status.textContent = "Testing the ICICI Breeze connection…";
    try {
      const result = await post("/breeze/test", breezeCredentials());
      S.breezeConnection = "connected";
      status.textContent = result.message || "Connected to ICICI Breeze.";
      status.className = "status pos";
    } catch (e) {
      S.breezeConnection = "failed";
      status.textContent = `Connection failed: ${e.message}`;
      status.className = "status err";
    } finally { button.disabled = false; }
  }

  function renderChatHistory() {
    const box = $("#fnoChatHistory");
    box.textContent = "";
    const messages = S.chat.length ? S.chat : [{ role: "assistant", content: "Try: “Is Breeze connected?”, “Show the current quote”, or “Explain my trade ideas”." }];
    messages.forEach(m => {
      const item = document.createElement("div");
      item.className = "fno-chat-msg " + m.role;
      item.textContent = m.content;
      box.appendChild(item);
    });
    box.scrollTop = box.scrollHeight;
  }

  function ideaChatSummary(idea) {
    const structure = idea.structure || {}, name = structure.name || "Strategy idea";
    const expiry = structure.legs && structure.legs[0] ? structure.legs[0].expiry : null;
    const legs = (idea.legs_detail || []).map(l => {
      const action = l.side > 0 ? "BUY" : "SELL";
      const contract = l.kind === "FUT" ? "future" : `${l.strike} ${l.kind === "CE" ? "call" : "put"}`;
      return `${action} ${contract} @ ${inr(l.price, 2)}`;
    }).join("; ");
    const risk = idea.risk || {}, plan = idea.exit_plan || {};
    const lines = [`${idea.symbol || "Stock"} — ${name}${fin(idea.score) ? `, score ${idea.score.toFixed(0)}/100` : ""}${expiry ? `, expiry ${fdate(expiry)}` : ""}.`];
    if (legs) lines.push("Legs: " + legs + ".");
    if (risk.max_loss != null) lines.push("Estimated maximum loss: " + inr(risk.max_loss, 0) + ".");
    if (plan.profit_target != null || plan.stop_loss != null) {
      lines.push(`App exit plan: target ${inr(plan.profit_target, 0)}, stop ${inr(plan.stop_loss, 0)}.`);
    }
    if (Array.isArray(idea.reasons) && idea.reasons.length) lines.push("Why it passed the rules: " + idea.reasons.slice(0, 2).join("; ") + ".");
    return lines.join(" ");
  }

  function chatMarketSummary(context) {
    const source = context.source === "breeze-live" ? "Breeze live (5-minute)" : context.source === "bhavcopy" ? "NSE bhavcopy (EOD)" : "synthetic demo";
    const stock = context.view === "scan" ? null : context.stock, scan = context.view === "scan" ? context.scan : null;
    if (stock && stock.issue) return `${stock.symbol || "This stock"} has a data issue: ${stock.issue.detail || stock.issue.title}. No trade was scored from this result.`;
    if (stock && stock.regime) {
      const g = stock.regime, oi = stock.oi || {}, values = [];
      if (g.spot != null) values.push("spot " + inr(g.spot, 2));
      if (g.trend) values.push("trend " + g.trend + (g.trend_score != null ? ` (${g.trend_score > 0 ? "+" : ""}${g.trend_score}/4)` : ""));
      if (g.rsi != null) values.push("RSI " + g.rsi.toFixed(0));
      if (g.iv != null) values.push("ATM IV " + pct(g.iv, 1));
      if (g.iv_rank != null) values.push("IV rank " + g.iv_rank.toFixed(0));
      else values.push("IV rank unavailable");
      if (g.rv20 != null) values.push("realized volatility " + pct(g.rv20, 1));
      if (oi.pcr != null) values.push("put/call OI ratio " + oi.pcr.toFixed(2));
      return `${stock.symbol} as of ${stock.asof || "the displayed date"} using ${source}: ${values.join("; ")}. ${context.quote && fin(context.quote.ltp) ? `Latest on-demand Breeze quote: ${inr(context.quote.ltp, 2)}.` : ""}`;
    }
    if (scan && scan.issue) return `The market scan has a data issue: ${scan.issue.detail || scan.issue.title}.`;
    if (scan && scan.market) {
      const m = scan.market;
      return `Market scan as of ${scan.asof || "the displayed date"} using ${source}: ${scan.eligible || 0} eligible stocks; ${m.bullish || 0} bullish, ${m.neutral || 0} range-bound, ${m.bearish || 0} bearish. Median IV rank ${m.median_iv_rank == null ? "unavailable" : m.median_iv_rank.toFixed(0)}, median put/call OI ratio ${m.median_pcr == null ? "unavailable" : m.median_pcr.toFixed(2)}.`;
    }
    if (context.quote && fin(context.quote.ltp)) return `No strategy snapshot is displayed. The latest on-demand Breeze quote for ${context.quote.symbol || "the selected stock"} is ${inr(context.quote.ltp, 2)}${context.quote.timestamp ? ` at ${context.quote.timestamp}` : ""}.`;
    return "No current market values are displayed yet. Load a stock quote, run a single-stock suggestion, or run a market scan first.";
  }

  function answerAppChat(question, context) {
    const q = String(question || "").trim().toLowerCase(), c = context || {};
    if (!q) return "Ask about the UI, workflow, Breeze connection, displayed values, or current trade ideas.";
    const stock = c.view === "scan" ? null : c.stock, scan = c.view === "scan" ? c.scan : null;
    const workflowQuestion = /\b(ui|interface|workflow|how (do|can|should)|steps|where|navigate|settings|tab|tabs|backtest|evaluate|single stock|market scan|sync|breeze live)\b/.test(q);
    if (workflowQuestion) {
      if (/\bsettings\b|credential|api key|session token/.test(q)) {
        return "Open Settings to configure a source, enter Breeze credentials, or test the connection. Credentials stay in page memory and are not saved by the app-only chat. Confidence: High — this is the displayed UI workflow.";
      }
      if (/\bbacktest\b/.test(q)) {
        return "Choose a stock, set the backtest inputs, and use Backtest to replay historical outcomes. Review assumptions and costs rather than treating past results as a guarantee. Confidence: Medium — use the exact current inputs shown.";
      }
      if (/\bevaluate\b/.test(q)) {
        return "Choose Evaluate to compare the app's generated outcomes against the selected evaluation inputs. It supports review; it is not a live-trade decision. Confidence: Medium — results depend on loaded data.";
      }
      if (/\bsync\b|latest bhavcopy|download data/.test(q)) {
        return "Open Settings and use Sync latest bhavcopy, then wait for the refresh to complete. Synced bhavcopy data is end-of-day settlement data, not live intraday data. Confidence: High — this is the displayed workflow.";
      }
      if (/\bbreeze live\b|live quote|refresh live/.test(q)) {
        return "Open Settings, test the Breeze connection, select Breeze live if supported, then use Refresh live quote. A timestamped quote is separate from EOD bhavcopy or synthetic demo data. Confidence: High — this is the displayed workflow.";
      }
      if (/\bmarket scan\b|\bscan\b/.test(q)) {
        return "Run Market scan from the Scan tab, use the bias/type filters, and review the qualifying shortlist. Then open a stock for trade details. Confidence: High — this describes the visible workflow.";
      }
      if (/\bsingle stock\b|single-stock|suggest trades|one stock/.test(q)) {
        return "Choose a stock, select bhavcopy or Breeze-live source, and use Suggest trades. Review the legs, maximum loss, exit plan, and score before any decision. Confidence: High — this describes the visible workflow.";
      }
      return "Brief workflow: sync bhavcopy or test Breeze in Settings; run Market scan or choose one stock; use Suggest trades; optionally open Backtest or Evaluate. Confidence: High — these controls are shown in the UI.";
    }
    if (/\b(score|100\/100|60\/100|shortlist score)\b/.test(q)) {
      return "A trade score is a rule-fit rating, not a win probability. Example: signal 25 + IV fit 20 + variance premium 10 + Greek fit 15 + trade quality 12 + OI 3 − costs 10 = 75/100. A scan shortlist score is a separate first-pass ranking using trend, IV, OI alignment, and liquidity. Confidence: High — this follows the app's displayed score components.";
    }
    const connectionQuestion = /\b(connection|connected|disconnect|authentication|authenticated|test connection)\b/.test(q)
      || (/\bbreeze\b/.test(q) && /\b(status|test|working|credential|api)\b/.test(q));
    if (connectionQuestion) {
      if (c.connection === "connected") return "The last Breeze connection test in this page succeeded. This is not a guarantee that the current feed is still available. Confidence: High — the result is directly displayed.";
      if (c.connection === "failed") return "The last Breeze connection test in this page failed. Check the message in Settings and test again. Confidence: High — the failure result is directly displayed.";
      return "Breeze has not been tested in this page yet. Open Settings and choose Test connection. Confidence: Medium — no test result is loaded.";
    }
    if (/\b(quote|ltp|price)\b/.test(q)) {
      if (c.quote && fin(c.quote.ltp)) {
        const change = fin(c.quote.change) ? `; change ${sgn(c.quote.change, 2)} (${sgn(c.quote.change_pct, 2)}%)` : "";
        return `Latest on-demand Breeze quote for ${c.quote.symbol || "the selected stock"}: LTP ${inr(c.quote.ltp, 2)}${change}${c.quote.timestamp ? `; timestamp ${c.quote.timestamp}` : ""}. Confidence: High — the quote is timestamped and loaded in the page.`;
      }
      if (stock && stock.regime && stock.regime.spot != null) return `No on-demand Breeze quote is loaded. The displayed ${stock.source === "breeze-live" ? "Breeze" : "strategy"} spot for ${stock.symbol} is ${inr(stock.regime.spot, 2)} as of ${stock.asof || "the displayed date"}. Confidence: Medium — this is a strategy snapshot, not a fresh quote.`;
      return "No quote is loaded. Choose a stock and press Refresh live quote, or run a suggestion to see its strategy spot. Confidence: Low — no quote is available.";
    }
    const valueQuestion = /\b(value|values|metric|metrics|market|data|trend|rsi|iv|volatility|pcr|put.call|open interest|spot)\b/.test(q);
    if (valueQuestion) {
      if (/\brsi\b/.test(q) && stock && stock.regime && stock.regime.rsi != null) {
        const rsi = stock.regime.rsi;
        return `Displayed RSI for ${stock.symbol} is ${rsi.toFixed(0)}. Values above 70 are commonly described as overbought and below 30 as oversold; RSI alone is not a trade signal.`;
      }
      if (/\biv rank\b/.test(q) && stock && stock.regime) {
        const rank = stock.regime.iv_rank;
        return `Displayed IV rank for ${stock.symbol} is ${rank == null ? "unavailable" : rank.toFixed(0) + "/100"}. ${stock.source === "breeze-live" ? "It uses only the recent option history returned by Breeze, not a one-year daily history." : "It compares implied volatility with the available stored history."}`;
      }
      if (/\bpcr\b|put.call|open interest/.test(q) && stock && stock.oi) {
        const oi = stock.oi;
        return `Displayed put/call open-interest ratio for ${stock.symbol} is ${oi.pcr == null ? "unavailable" : oi.pcr.toFixed(2)}${oi.buildup_label ? `; ${oi.buildup_label}` : ""}. Open interest is context, not proof of market direction.`;
      }
      if (/\btrend\b/.test(q) && stock && stock.regime) {
        const g = stock.regime;
        return `${stock.symbol}'s displayed trend is ${g.trend || "unavailable"}${g.trend_score != null ? ` (${g.trend_score > 0 ? "+" : ""}${g.trend_score} of ±4)` : ""}, based on the app's technical rules. ${g.rsi != null ? `RSI is ${g.rsi.toFixed(0)}.` : ""}`;
      }
      return chatMarketSummary(c);
    }
    const tradeQuestion = /\b(trade|trades|idea|ideas|strategy|strategies|suggest|suggestion|recommend|position|option|call|put)\b/.test(q);
    if (tradeQuestion) {
      if (stock) {
        if (stock.issue) return chatMarketSummary(c);
        const ideas = Array.isArray(stock.suggestions) ? stock.suggestions : [];
        if (!ideas.length) return `${stock.symbol || "The selected stock"} has no qualifying trade idea in the current result. ${stock.note || "Staying out is a valid outcome."}`;
        return `Current app-generated idea${ideas.length === 1 ? "" : "s"} for ${stock.symbol}:\n${ideas.slice(0, 3).map(ideaChatSummary).join("\n")}\nThese are rule-based information, not a personal recommendation or an order. Verify current prices and risk before trading. Confidence: Medium — the ideas match app rules, but market and execution risk remain.`;
      }
      if (!scan) return "There is no current market scan to summarize. Choose a stock and run Suggest trades, or run a market scan first.";
      if (scan.issue) return chatMarketSummary(c);
      const ideas = Array.isArray(scan.ideas) ? filterIdeas(scan.ideas, c.filter || { bias: "all", kind: "all" }) : [];
      if (!ideas.length) return `${scan.note || "The current market scan has no qualifying trade ideas."}${scan.top_shortlist && scan.top_shortlist.length ? ` Closest shortlist: ${scan.top_shortlist.slice(0, 3).map(x => `${x.symbol} (${x.bias})`).join(", ")}.` : ""}`;
      return `Top current market-scan ideas matching the visible filters:\n${ideas.slice(0, 3).map(ideaChatSummary).join("\n")}\nThese are rule-based information, not a personal recommendation or an order. Verify current prices and risk before trading. Confidence: Medium — the scan reflects app rules and loaded data.`;
    }
    return "I answer briefly about the UI, workflow, displayed data, and current trade ideas. Try asking “How do I run a market scan?”, “What is the current quote?”, “Show the IV rank”, or “Explain my trade ideas”. Confidence: Low — I could not match the question to a supported workflow.";
  }

  function chatContextText() {
    const context = {
      view: S.view, source: S.source, connection: S.breezeConnection, quote: S.liveQuote,
      stock: S.sug, scan: S.scan, filter: S.filter
    };
    const lines = [
      "UI workflow: Settings syncs bhavcopy or configures Breeze and tests the connection; Market scan scans eligible stocks; a selected stock can run Suggest trades; Backtest and Evaluate provide historical and outcome review.",
      chatMarketSummary(context)
    ];
    if (S.view === "scan" && S.scan && Array.isArray(S.scan.ideas)) {
      lines.push("Visible scan ideas:\n" + filterIdeas(S.scan.ideas, S.filter).slice(0, 3).map(ideaChatSummary).join("\n"));
    } else if (S.sug && Array.isArray(S.sug.suggestions)) {
      lines.push("Current stock ideas:\n" + S.sug.suggestions.slice(0, 3).map(ideaChatSummary).join("\n"));
    }
    return lines.join("\n\n").slice(0, 18000);
  }

  const chatModels = { openai: "gpt-4.1-mini", anthropic: "claude-haiku-4-5-20251001", gemini: "gemini-2.5-flash" };

  function updateChatMode() {
    const nextMode = val("#fnoChatMode");
    if (S.chatMode === "model" && nextMode !== "model") $("#fnoChatApiKey").value = "";
    S.chatMode = nextMode;
    $("#fnoChatModelOptions").hidden = S.chatMode !== "model";
    $("#fnoChatStatus").textContent = "";
    $("#fnoChatStatus").className = "status";
  }

  function updateChatProvider() {
    $("#fnoChatModelName").value = chatModels[val("#fnoChatProvider")];
    $("#fnoChatApiKey").value = "";
  }

  async function sendChat(event) {
    event.preventDefault();
    const question = val("#fnoChatMessage").trim();
    if (!question) return;
    const status = $("#fnoChatStatus");
    const send = $("#fnoChatSend"), input = $("#fnoChatMessage");
    if (S.chatMode === "model" && !val("#fnoChatApiKey").trim()) {
      status.textContent = "Enter the selected provider's API key to use model mode.";
      status.className = "status err";
      $("#fnoChatApiKey").focus();
      return;
    }
    S.chat.push({ role: "user", content: question });
    input.value = "";
    renderChatHistory();
    if (S.chatMode !== "model") {
      S.chat.push({ role: "assistant", content: answerAppChat(question, {
        view: S.view, source: S.source, connection: S.breezeConnection, quote: S.liveQuote,
        stock: S.sug, scan: S.scan, filter: S.filter
      }) });
      status.textContent = "Answered from app data only; no model or external service was used.";
      status.className = "status";
    } else {
      send.disabled = true;
      input.disabled = true;
      status.textContent = `Sending to ${$("#fnoChatProvider").selectedOptions[0].text}…`;
      status.className = "status";
      try {
        const messages = S.chat.slice(-12).map(m => ({ role: m.role, content: m.content }));
        const result = await post("/chat", {
          provider: val("#fnoChatProvider"),
          model: val("#fnoChatModelName").trim(),
          api_key: val("#fnoChatApiKey"),
          messages,
          context: chatContextText()
        });
        S.chat.push({ role: "assistant", content: result.text });
        status.textContent = `Answered by ${result.provider} (${result.model}). The provider received your prompt and displayed app context.`;
        status.className = "status";
      } catch (e) {
        S.chat.push({ role: "assistant", content: `Model request failed: ${e.message}` });
        status.textContent = e.message;
        status.className = "status err";
      } finally {
        send.disabled = false;
        input.disabled = false;
      }
    }
    while (S.chat.length > 20) S.chat.shift();
    renderChatHistory();
    input.focus();
  }

  async function loadExpiryOptions() {
    const wrap = $("#fnoExpiryWrap"), sel = $("#fnoExpiry");
    const enabled = S.mode === "market" ? S.source === "bhavcopy" : !!val("#fnoSym").trim();
    wrap.style.display = enabled ? "" : "none";
    if (!enabled) { sel.disabled = true; return; }
    const keep = sel.value;
    sel.disabled = true;
    sel.innerHTML = `<option value="">Loading expiries…</option>`;
    try {
      const query = new URLSearchParams({ source: S.source });
      if (S.source !== "breeze-live" && val("#fnoAsof")) query.set("asof", val("#fnoAsof"));
      if (S.mode === "one" && val("#fnoSym").trim()) query.set("symbol", val("#fnoSym").trim().toUpperCase());
      const result = await api("/expiries?" + query.toString());
      const expiries = Array.isArray(result.expiries) ? result.expiries : [];
      sel.innerHTML = expiries.length
        ? expiries.map(d => `<option value="${esc(d)}">${esc(fdate(d))}</option>`).join("")
        : `<option value="">No expiries available</option>`;
      sel.value = expiries.includes(keep) ? keep : (expiries[0] || "");
      sel.disabled = expiries.length === 0;
      $("#fnoExpiryHint").textContent = S.source === "breeze-live"
        ? result.expiry_source === "bhavcopy"
          ? `Upcoming expiries for ${val("#fnoSym").trim().toUpperCase()} from loaded NSE option data; Breeze must return a live chain for the selected expiry.`
          : `Estimated monthly expiries for ${val("#fnoSym").trim().toUpperCase()} (no loaded NSE contracts); verify against Breeze.`
        : S.mode === "one" ? `Expiries available for ${val("#fnoSym").trim().toUpperCase()} on the selected day.`
        : "Only expiries with option data on the selected day are listed.";
      if (!expiries.length) say("#fnoMsg", "No upcoming option expiries are available for this day.", "err");
    } catch (e) {
      sel.innerHTML = `<option value="">Could not load expiries</option>`;
      sel.disabled = true;
      say("#fnoMsg", esc(e.message), "err");
    }
  }

  /** Downloads the chosen history, one window at a time so each request is short. Returns true when it finished without an error. */
  async function doSync(initialBody = { history_days: +val("#fnoHist") || 365 }) {
    const buttons = [$("#fnoSyncLatest"), $("#fnoSync")], stop = $("#fnoStop");
    S.syncStop = false; buttons.forEach(button => { button.disabled = true; }); stop.hidden = false;
    let body = initialBody, got = 0, holidays = 0, err = null, stopped = false, last = null, suspect = null;
    try {
      for (let i = 0; i < 80; i++) {
        say("#fnoDataMsg", busy(got ? `Downloaded ${got} days so far… (about a second a day)` : "Downloading from NSE… (about a second a day)"));
        const r = await post("/bhavcopy/sync", body); last = r; S.status = r;
        got += (r.counts || {}).downloaded || 0; holidays += (r.counts || {}).no_file || 0; renderStatus();
        if (r.error) { err = r.error; break; }
        if (r.suspect_missing) { suspect = r; break; }
        if (!r.more_to_sync) break;
        if (S.syncStop) { stopped = true; break; }
        body = { start: r.next_start, end: r.end };
      }
    } catch (e) { err = e.message; } finally { buttons.forEach(button => { button.disabled = false; }); stop.hidden = true; }
    const total = S.status && S.status.bhavcopy ? S.status.bhavcopy.days : 0;
    if (suspect) {
      const n = (suspect.counts || {}).no_file || 0;
      say("#fnoDataMsg", `${got ? `Downloaded ${got} days, then ` : ""}NSE had <b>no file for any of the ${n} weekdays</b> from ${esc(fdate(suspect.range[0]))} to ${esc(fdate(suspect.range[1]))}. Holidays explain only a few days a year, so the download address may be wrong or changed, or NSE does not publish files that far back. I stopped instead of repeating the failing request. Save a file from nseindia.com in your browser and use <i>Import file</i>.`, "err");
    } else if (err) say("#fnoDataMsg", `${got ? `Downloaded ${got} days before stopping. ` : ""}<span class="err">${esc(err)}</span>`, "err");
    else if (stopped) say("#fnoDataMsg", `Stopped after ${got} new days (${total} stored). Press Sync again to carry on.`);
    else say("#fnoDataMsg", got ? `Downloaded ${got} new days${holidays ? ` (${holidays} weekday${holidays > 1 ? "s" : ""} with no file${holidays > 12 && holidays > 0.15 * (got + holidays) ? ": more than holidays usually explain, so check the data" : ", normally holidays"})` : ""}. ${total} days stored.` : (last && last.message) || `Already up to date: ${total} days stored.`);
    await loadSymbols();
    return !err && !stopped;
  }

  async function doImport(file) {
    if (!file) return;
    say("#fnoDataMsg", busy("Importing " + file.name + "…"));
    try {
      const buf = await file.arrayBuffer();
      const r = await api("/bhavcopy/import?name=" + encodeURIComponent(file.name), { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: buf });
      S.status = r; say("#fnoDataMsg", `Imported ${esc(fdate(r.imported.date))}: ${esc(r.imported.message)}.`);
      renderStatus(); await loadSymbols();
    } catch (e) { say("#fnoDataMsg", esc(e.message), "err"); }
    $("#fnoFile").value = "";
  }

  function params() {
    return { source: S.source, symbol: val("#fnoSym").trim().toUpperCase(), asof: val("#fnoAsof") || undefined, risk_budget: +val("#fnoRisk") || 0,
             min_score: +val("#fnoMin"), min_volume: +val("#fnoVol") || 0, min_oi: +val("#fnoOi") || 0,
             allow_futures: S.source === "breeze-live" ? false : $("#fnoFut").checked, iv_scale: +val("#fnoIv") || 1,
             ...(S.source === "breeze-live" ? breezeCredentials() : {}) };
  }

  function setTab(t) {
    if (t === "bt" && S.source === "breeze-live") return;
    S.tab = t;
    S.host.querySelectorAll(".fno-tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
    $("#fnoPanSug").hidden = t !== "sug"; $("#fnoPanBt").hidden = t !== "bt"; $("#fnoPanLog").hidden = t !== "log";
    $("#fnoPanSettings").hidden = t !== "settings";
  }

  /* ------------------------------------------------------------------ suggestions */
  async function runSuggest() {
    const p = params(); if (!p.symbol) { say("#fnoMsg", "Enter a symbol.", "err"); return; }
    if (S.source === "breeze-live") delete p.asof;
    await loadExpiryOptions();
    p.expiry = val("#fnoExpiry");
    if (!p.expiry) { say("#fnoMsg", "No option expiry is available for this symbol and date.", "err"); return; }
    const b = $("#fnoRunSug"); b.disabled = true; setTab("sug"); say("#fnoMsg", busy("Reading the chain and scoring trades…"));
    try { S.sug = await post("/suggest", p); say("#fnoMsg", ""); renderSug(); } catch (e) { say("#fnoMsg", esc(e.message), "err"); } finally { b.disabled = false; }
  }

  function regimeStrip(r, oi, live) {
    if (!r) return "";
    const tcls = r.trend === "bullish" ? "good" : r.trend === "bearish" ? "bad" : "";
    const pos = (r.trend_score + 4) / 8 * 100, ivr = r.iv_rank;
    const period = live ? "5-minute bars" : "days";
    return `<div class="strip">
<div class="tile"><div class="l"><span>Trend</span><span class="badge ${tcls}">${esc(r.trend)}</span></div><div class="v">${r.trend_score > 0 ? "+" : ""}${r.trend_score} <span class="mute" style="font-size:12px;font-weight:400">of ±4</span></div>
  <div class="track"><div class="mid"></div><div class="dot" style="left:${pos}%"></div></div><div class="s">SMA20/50 ${period}, close vs SMA20, MACD, RSI zone</div></div>
<div class="tile"><div class="l"><span>IV rank${live ? " (recent 5-minute history)" : ""}</span></div><div class="v">${ivr == null ? "n/a" : ivr.toFixed(0)}</div>
  ${ivr == null ? `<div class="s">${live ? "Insufficient aligned Breeze option history; IV rank is unknown." : "No IV history, so IV rank is unknown."}</div>` : `<div class="track iv"><div class="dot" style="left:${clamp(ivr, 0, 100)}%"></div></div><div class="s">${live ? "recent IV samples · " : ""}${r.iv_percentile != null ? "percentile " + r.iv_percentile.toFixed(0) + " · " : ""}${ivr >= 50 ? "premium is rich" : ivr <= 30 ? "premium is cheap" : "middle of its range"}</div>`}</div>
<div class="tile"><div class="l"><span>Volatility</span></div><div class="v">${r.iv ? pct(r.iv) : "—"} <span class="mute" style="font-size:12px;font-weight:400">ATM IV</span></div>
  <div class="s">${live ? "20-bar" : "20-day"} realised ${r.rv20 ? pct(r.rv20) : "—"}${r.vrp != null ? " · premium " + sgn(r.vrp * 100, 1) + " pts" : ""}</div></div>
<div class="tile"><div class="l"><span>RSI (14)</span></div><div class="v ${r.rsi > 70 ? "warn" : r.rsi < 30 ? "warn" : ""}">${r.rsi.toFixed(0)}</div><div class="s">${r.rsi > 70 ? "overbought" : r.rsi < 30 ? "oversold" : "neutral zone"} · ATR ${r.atr_pct.toFixed(1)}%${live ? " per bar" : " a day"}</div></div>
<div class="tile"><div class="l"><span>Spot</span></div><div class="v">${inr(r.spot, 2)}</div><div class="s">swing range ${inr(r.swing_low)} to ${inr(r.swing_high)}</div></div>
${oi ? `<div class="tile"><div class="l"><span>Open interest</span></div><div class="v">PCR ${oi.pcr != null ? oi.pcr.toFixed(2) : "—"}</div><div class="s">${oi.buildup_label ? esc(oi.buildup_label) + " · " : ""}${oi.put_wall ? "put wall " + strikeTxt(oi.put_wall.strike) : ""}${oi.put_wall && oi.call_wall ? " · " : ""}${oi.call_wall ? "call wall " + strikeTxt(oi.call_wall.strike) : ""}</div></div>` : ""}</div>`;
  }

  function legsTable(s) {
    const rows = s.legs_detail.map(l => {
      const what = l.kind === "FUT" ? `Future <span class="mute">${esc(sdate(l.expiry))}</span>` : `${esc(sdate(l.expiry))} <b>${l.strike}</b> ${l.kind === "CE" ? "Call" : "Put"}`;
      return `<tr><td><span class="badge ${l.side > 0 ? "buy" : "sell"}">${l.side > 0 ? "BUY" : "SELL"}</span></td><td>${what}</td><td class="r">${inr(l.price, 2)}</td><td class="r">${l.iv ? pct(l.iv) : "—"}</td><td class="r">${sgn(l.delta, 2)}</td></tr>`;
    }).join("");
    const lots = s.structure.legs[0].lots, unit = s.structure.lot_size;
    return `<table class="compact"><thead><tr><th>Action</th><th>Contract</th><th class="r">Price</th><th class="r">IV</th><th class="r">Δ</th></tr></thead><tbody>${rows}</tbody></table>
<div class="hint">${lots} lot${lots > 1 ? "s" : ""} × ${unit} = ${(lots * unit).toLocaleString("en-IN")} units. Prices are fills after assumed slippage.</div>`;
  }

  function greekTiles(g, fut) {
    const T = (l, v, s, tip) => `<div class="mini" title="${esc(tip)}"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
    return `<div class="grid4">` +
      T("Delta", sgn(g.delta, 0), "units of the stock", "How many shares the position behaves like. Positive gains when the stock rises.") +
      (fut ? T("Gamma", "none", "futures have none", "Futures have no gamma, theta or vega.") : T("Gamma", sgn(g.gamma, 2), g.gamma >= 0 ? "long: gains from big moves" : "short: hurt by big moves", "How fast delta changes per ₹1 move. Short gamma is paid theta to absorb moves.")) +
      (fut ? T("Theta", "none", "no time decay", "") : T("Theta / day", sinr(g.theta), g.theta >= 0 ? "you collect decay" : "you pay decay", "Rupees gained or lost per calendar day from time passing, other things equal.")) +
      (fut ? T("Vega", "none", "no IV exposure", "") : T("Vega / IV pt", sinr(g.vega), g.vega >= 0 ? "wins if IV rises" : "wins if IV falls", "Rupees gained per one point rise in implied volatility.")) + `</div>`;
  }

  function riskTiles(s) {
    const r = s.risk, fut = s.kind === "futures", single = ["Long call", "Long put"].includes(s.structure.name), p = s.exit_plan;
    const T = (l, v, sub) => `<div class="mini"><div class="l">${l}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
    return `<div class="grid4">` +
      (fut ? T("Target", sinr(p.profit_target), "plan, not a cap") + T("Stop", sinr(p.stop_loss), "plan; gaps can pass it")
           : T("Max profit", single || r.max_profit == null ? "open-ended" : sinr(r.max_profit), single ? "premium multiple is the target" : "") + T("Max loss", sinr(r.max_loss), "worst case at expiry")) +
      (fut ? T("Net premium", "none", "") + T("Breakeven", r.breakevens.length ? inr(r.breakevens[0], 2) : "—", "")
           : T(r.net_premium >= 0 ? "Credit received" : "Debit paid", inr(Math.abs(r.net_premium)), "") + T("Win chance", s.pop == null ? "—" : pct(s.pop), "model-implied, not a forecast")) +
      `</div><div class="hint">${fut ? "" : "Breakeven" + (r.breakevens.length > 1 ? "s " : " ") + r.breakevens.map(b => inr(b, 2)).join(" and ") + " · "}round-trip costs about ${inr(r.cost_roundtrip)}</div>`;
  }

  function scoreBlock(s) {
    const sp = s.risk.score_parts || {};
    const pos = PARTS.map(([k, label, max]) => `<div class="part"><span>${label}</span><span class="t"><i style="width:${clamp((sp[k] || 0) / max * 100, 0, 100)}%"></i></span><span>${(sp[k] || 0).toFixed(1)}<span class="mute">/${max}</span></span></div>`).join("");
    const neg = PENALTIES.filter(([k]) => sp[k]).map(([k, label]) => `<div class="part neg"><span>${label}</span><span class="t"><i style="width:${clamp(-sp[k] / 15 * 100, 0, 100)}%"></i></span><span>${sp[k].toFixed(1)}</span></div>`).join("");
    const oi = sp.oi ? `<div class="part ${sp.oi < 0 ? "neg" : ""}"><span>Open interest</span><span class="t"><i style="width:${clamp(Math.abs(sp.oi) / 20 * 100, 0, 100)}%"></i></span><span>${sp.oi > 0 ? "+" : ""}${sp.oi.toFixed(1)}</span></div>` : "";
    return `<div class="parts">${pos}${oi}${neg}</div>`;
  }

  function verdict(s, quiet) {
    const r = s.risk, p = s.exit_plan, single = ["Long call", "Long put"].includes(s.structure.name);
    if (s.kind === "futures") return `<div class="verdict"><div class="big">Plan to make about <b>${inr(p.profit_target)}</b> or cut the loss at <b>${inr(-p.stop_loss)}</b></div><div class="sub">Futures profit and loss are not capped; a gap can pass the stop.</div></div>`;
    const up = single || r.max_profit == null ? `profit is open-ended (plan: take ${inr(p.profit_target)})` : `make up to <b>${inr(r.max_profit)}</b>`;
    return `<div class="verdict"><div class="big">Risk <b>${inr(-r.max_loss)}</b> to ${up}</div>
<div class="sub">${s.pop == null ? "" : `<b>${pct(s.pop)}</b> model chance of profit · `}${r.breakevens.length ? "breakeven " + r.breakevens.map(b => inr(b)).join(" / ") + " · " : ""}${r.net_premium >= 0 ? "you receive " : "you pay "}${inr(Math.abs(r.net_premium))} · costs about ${inr(r.cost_roundtrip)}</div>
${quiet ? "" : `<div class="sub">Margin is not calculated here: check your broker's margin calculator before trading.</div>`}</div>`;
  }

  function oiChips(s) {
    const o = s.risk && s.risk.oi; if (!o) return "";
    const out = [];
    if (o.buildup) out.push(`<span class="oichip ${["long_buildup", "short_covering"].includes(o.buildup) ? "up" : "down"}" title="Futures price ${sgn(o.fut_price_chg * 100, 1)}%, open interest ${sgn(o.fut_oi_chg * 100, 1)}%">${esc(o.buildup_label)}${o.buildup_strength === "strong" ? " ▲▲" : ""}</span>`);
    if (o.pcr != null) out.push(`<span class="oichip" title="Total put open interest divided by total call open interest for this expiry">PCR ${o.pcr.toFixed(2)}</span>`);
    if (o.put_wall) out.push(`<span class="oichip" title="The strike with the most put open interest below the stock: support writers defend">put wall ${strikeTxt(o.put_wall.strike)}</span>`);
    if (o.call_wall) out.push(`<span class="oichip" title="The strike with the most call open interest above the stock: resistance writers defend">call wall ${strikeTxt(o.call_wall.strike)}</span>`);
    out.push(`<span class="oichip ${o.points > 0 ? "up" : o.points < 0 ? "down" : ""}" title="How much open interest added to or took from this trade's score">OI ${o.points > 0 ? "+" : o.points < 0 ? "−" : ""}${Math.abs(o.points).toFixed(0)} pts</span>`);
    return `<div class="oichips">${out.join("")}</div>`;
  }

  function oiBlock(s) {
    const o = s.risk && s.risk.oi;
    if (!o) return `<div class="hint">No open-interest data for this source.</div>`;
    const wall = (w, side) => w ? `${strikeTxt(w.strike)} · ${(w.share * 100).toFixed(0)}% of all ${side} OI${w.change_pct != null ? " · " + sgn(w.change_pct * 100, 0) + "% today" : ""}` : "none within 15% of the stock";
    const rows = [["Put/call OI ratio", o.pcr != null ? o.pcr.toFixed(2) : "—"], ["Call OI wall (resistance)", wall(o.call_wall, "call")], ["Put OI wall (support)", wall(o.put_wall, "put")],
      ["Max pain", o.max_pain != null ? strikeTxt(o.max_pain) : "—"],
      ["Futures price / OI today", o.fut_price_chg != null ? `${sgn(o.fut_price_chg * 100, 1)}% / ${sgn(o.fut_oi_chg * 100, 1)}%${o.buildup_label ? " · " + o.buildup_label : ""}` : "no previous day to compare"],
      ["Points added to the score", `${o.points > 0 ? "+" : ""}${o.points}${Object.keys(o.parts || {}).length ? " (" + Object.entries(o.parts).map(([k, v]) => k.replace("_", " ") + " " + (v > 0 ? "+" : "") + v).join(", ") + ")" : ""}`]];
    return `<table class="compact"><tbody>${rows.map(([k, v]) => `<tr><td class="mute">${k}</td><td>${esc(v)}</td></tr>`).join("")}</tbody></table>
<div class="hint">Open interest shows positions, not who is right: a "wall" can be written by someone hedged elsewhere. Points are small on purpose.</div>`;
  }

  /** opt.scan: compact card for the market list (stock name first, details folded away, extra actions). */
  function suggestionCard(s, i, opt) {
    opt = opt || {};
    const kind = s.kind, p = s.exit_plan, inLog = S.log.some(e => sameKey(e, s)), key = s.symbol + "|" + s.structure.name, open = !opt.scan || S.open[key];
    const badge = kind === "credit" ? `<span class="badge info" title="You receive premium up front">CREDIT</span>` : kind === "debit" ? `<span class="badge warn" title="You pay premium up front">DEBIT</span>` : `<span class="badge">FUTURES</span>`;
    const o = s.risk && s.risk.oi;
    const marks = [{ x: s.payoff.spot, label: "spot", color: "#24392c", row: 0 }].concat((s.risk.breakevens || []).map(b => ({ x: b, label: "breakeven", color: "#825600", row: 1 })))
      .concat(o && o.call_wall ? [{ x: o.call_wall.strike, label: "call wall", color: "#b42332", row: 2 }] : []).concat(o && o.put_wall ? [{ x: o.put_wall.strike, label: "put wall", color: "#176b44", row: 2 }] : []);
    const chart = lineChart({ xs: s.payoff.price, series: [{ name: "At expiry", ys: s.payoff.expiry, color: "#2f704b", width: 2.4 }, { name: "Today (model)", ys: s.payoff.now, color: "#879b8b", dash: "5 4", width: 1.8 }],
      zero: true, fill: 0, marks, height: 280, xfmt: v => Math.round(v).toLocaleString("en-IN"), xtip: v => "Stock at ₹" + v.toLocaleString("en-IN", { maximumFractionDigits: 0 }), aria: "Profit and loss against the stock price" });
    const warns = (s.warnings || []).map(w => `<div class="note">${esc(w)}</div>`).join("");
    const chips = `<div class="chips"><div class="chip good"><span>Take profit at</span><b>${sinr(p.profit_target)}</b></div><div class="chip bad"><span>Cut loss at</span><b>${sinr(p.stop_loss)}</b></div><div class="chip"><span>Latest exit</span><b>${p.exit_dte} days before expiry</b></div></div>` +
      (p.invalidation ? `<div class="hint">Also leave if the stock closes ${esc(p.invalidation[0])} ${inr(p.invalidation[1], 2)}: the idea no longer holds.</div>` : "");
    const words = `<ul class="why plain">${plainWords(s).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
    const alts = opt.scan && s.scan && s.scan.alternatives && s.scan.alternatives.length ? `<div class="hint">Also considered for ${esc(s.symbol)}: ${s.scan.alternatives.map(a => esc(a.name) + " (" + a.score + ")").join(", ")}</div>` : "";
    const bias = s.scan && s.scan.bias ? `<span class="badge ${s.scan.bias === "bullish" ? "good" : s.scan.bias === "bearish" ? "bad" : ""}">${s.scan.bias === "neutral" ? "range-bound" : esc(s.scan.bias)}</span>` : "";
    const more = `<div class="sug-body"><div><div class="sec-title" style="margin-top:0">What to trade</div>${legsTable(s)}<div class="sec-title">In plain words</div>${words}</div>
  <div><div class="sec-title" style="margin-top:0">Profit and loss against the stock price</div>${chart}</div></div>
<div style="padding:0 18px 4px"><div class="sec-title" style="margin-top:0">When to get out</div>${chips}${alts}</div>
<div style="padding:12px 18px 0"><details><summary>Why this trade: Greeks, open interest, score and reasoning</summary>
  <div class="sug-foot" style="padding:0 0 8px"><div><div class="sec-title" style="margin-top:0">The Greeks</div>${greekTiles(s.greeks, kind === "futures")}</div>
  <div><div class="sec-title" style="margin-top:0">Open interest</div>${oiBlock(s)}</div>
  <div><div class="sec-title" style="margin-top:0">Why this score</div>${scoreBlock(s)}</div>
  <div><div class="sec-title" style="margin-top:0">Reasoning</div><ul class="why">${s.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div></div>${warns}</details></div>`;
    const acts = `<div class="actions"><button class="small" data-act="log" ${inLog ? "disabled" : ""}>${inLog ? "In my log" : "Add to my log"}</button><button class="small" data-act="copy">Copy as text</button>` +
      (opt.scan ? `<button class="small" data-act="stock">Look at ${esc(s.symbol)} on its own</button><button class="small" data-act="bt">Backtest ${esc(s.symbol)}</button>` : "") + `</div>`;
    return `<div class="sug ${opt.scan ? "compact" : ""}" data-i="${i}" data-key="${esc(key)}">
<div class="sug-head"><span class="rank">#${opt.rank || i + 1}</span>${opt.scan ? `<span class="sym">${esc(s.symbol)}</span>` : ""}<span class="name">${esc(s.structure.name)}</span>${badge}${bias}
  <span class="meta">${opt.scan ? "" : esc(s.symbol) + " · "}expiry ${esc(fdate(s.structure.legs[0].expiry))} · ${s.dte} days left</span>
  <span class="score" title="How well the trend, IV level, Greeks, open interest and costs line up (0 to 100)"><span class="bar"><i style="width:${clamp(s.score, 0, 100)}%"></i></span>${s.score.toFixed(0)}<span class="mute">/100</span></span></div>
${verdict(s, opt.scan)}${oiChips(s) ? `<div style="padding:8px 18px 0">${oiChips(s)}</div>` : ""}
${opt.scan ? `<div style="padding:10px 18px 12px"><button class="small" data-act="toggle">${open ? "Hide the trade" : "Show the trade: legs, chart, exit plan"}</button></div>` : ""}
<div class="sug-more" ${open ? "" : "hidden"}>${more}${acts}</div></div>`;
  }

  /** A missing-data problem: says what is wrong, how far off we are, and offers the fix. Never worded as "stay out". */
  function issuePanel(i, retry) {
    const unit = i.unit || "trading days";
    const meter = i.code === "insufficient_history" ? `<div class="meter"><i style="width:${clamp(i.have / i.need * 100, 0, 100)}%"></i></div><div class="hint">${i.have} of the ${i.need} ${esc(unit)} the rules need${i.recommended > i.need ? ` (${i.recommended} is best)` : ""}</div>` : "";
    const fix = i.fix === "sync" ? `<div class="btns"><button class="primary" style="width:auto" data-fix="sync">Download 1 year from NSE, then retry</button></div>`
      : i.fix === "index" ? `<div class="btns"><button class="primary" style="width:auto" data-fix="index">Build the index, then scan</button></div><div class="hint">${i.missing} of ${i.total} stored days to index. You can keep using the app while it runs.</div>` : "";
    return `<div class="panel issue"><h3>${esc(i.title)}</h3><div style="font-size:14px;line-height:1.55;margin-bottom:10px">${esc(i.detail)}</div>${meter}${fix}</div>`;
  }
  function wireFix(el, retry) {
    const b = el.querySelector('[data-fix="sync"]');
    if (b) b.addEventListener("click", async () => { $("#fnoHist").value = "365"; b.disabled = true; if (await doSync()) retry(); else b.disabled = false; });
    const ib = el.querySelector('[data-fix="index"]');
    if (ib) ib.addEventListener("click", async () => { ib.disabled = true; try { await doIndex(); say("#fnoMsg", ""); retry(); } catch (e) { say("#fnoMsg", esc(e.message), "err"); ib.disabled = false; } });
  }
  /** Builds the per-stock summary index a batch at a time (each request is short), reporting progress. */
  async function doIndex() {
    for (let i = 0; i < 60; i++) {
      const r = await post("/index", { max_days: 40 });
      say("#fnoMsg", busy(`Indexing stored days… ${r.total - r.remaining} of ${r.total}`));
      if (!r.remaining) return true;
    }
    throw new Error("The index did not finish. Try again.");
  }

  function renderSug() {
    const r = S.sug, el = $("#fnoPanSug");
    if (!r) { el.innerHTML = `<div class="empty">Type an NSE symbol and press <b>Suggest trades</b> to look at one stock in detail.</div>`; return; }
    let h = `<h2 style="font-size:15px;margin:0 0 4px">${esc(r.symbol)} <span class="mute" style="font-weight:400">as of ${esc(fdate(r.asof))}${r.expiry ? " · trade expiry " + esc(fdate(r.expiry)) : ""}</span></h2>`;
    if (r.issue) { el.innerHTML = h + issuePanel(r.issue) + (r.data_notes || []).map(n => `<div class="note info">${esc(n)}</div>`).join(""); wireFix(el, runSuggest); return; }
    const notes = r.data_notes || [];
    h += (r.source === "synthetic" ? notes.map(n => `<div class="note bad">${esc(n)}</div>`).join("")
      : notes.length ? `<details><summary>About this data (${notes.length} note${notes.length > 1 ? "s" : ""})</summary>${notes.map(n => `<div class="note info">${esc(n)}</div>`).join("")}</details>` : "") + regimeStrip(r.regime, r.oi, r.source === "breeze-live");
    if (!r.suggestions.length) h += `<div class="panel"><h3>No trade</h3><div style="font-size:15px;margin-bottom:6px">${esc(r.note || "Nothing passed the filters.")}</div><div class="hint">Staying out is a valid output. The rules only suggest a trade when the trend, the IV level, the Greeks and the costs agree.</div></div>`;
    h += r.suggestions.map((x, i) => suggestionCard(x, i, {})).join("");
    if (r.rejected.length) h += `<details><summary>${r.rejected.length} candidate${r.rejected.length > 1 ? "s" : ""} considered and rejected</summary><ul class="why">${r.rejected.map(x => `<li><b>${esc(x.structure)}</b>: ${esc(x.reason)}</li>`).join("")}</ul></details>`;
    el.innerHTML = h; hookCharts(el);
    el.querySelectorAll(".sug").forEach(card => {
      const s = r.suggestions[+card.dataset.i];
      wireCard(card, s);
    });
  }

  function wireCard(card, s) {
    const on = (act, fn) => { const b = card.querySelector(`[data-act="${act}"]`); if (b) b.addEventListener("click", fn); };
    on("log", ev => { addToLog(s); ev.target.disabled = true; ev.target.textContent = "In my log"; });
    on("copy", ev => copyText(textOf(s), ev.target));
    on("toggle", ev => { const key = card.dataset.key, more = card.querySelector(".sug-more"); more.hidden = !more.hidden; S.open[key] = !more.hidden; ev.target.textContent = more.hidden ? "Show the trade: legs, chart, exit plan" : "Hide the trade"; });
    on("stock", () => openStock(s.symbol, false));
    on("bt", () => openStock(s.symbol, true));
  }

  function textOf(s) {
    const L = s.legs_detail.map(l => `  ${l.side > 0 ? "BUY " : "SELL"} ${l.kind === "FUT" ? "FUT" : l.strike + " " + l.kind} @ ${l.price}`).join("\n"), r = s.risk, p = s.exit_plan;
    return `${s.structure.name} on ${s.symbol} (as of ${s.asof}, expiry ${s.structure.legs[0].expiry}, ${s.structure.legs[0].lots} lot x ${s.structure.lot_size})\n${L}\n` +
      `Score ${s.score}/100 | max loss ${r.max_loss == null ? "n/a" : Math.round(-r.max_loss)} | model win chance ${s.pop == null ? "n/a" : pct(s.pop)}\n` +
      `Exit: profit ${Math.round(p.profit_target)}, stop ${Math.round(p.stop_loss)}, close ${p.exit_dte} days before expiry\nRule-based output, information only, not investment advice.`;
  }
  function copyText(t, btn) {
    const done = () => { const o = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = o; }, 1400); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, () => fallbackCopy(t, done)); else fallbackCopy(t, done);
  }
  function fallbackCopy(t, done) { const a = document.createElement("textarea"); a.value = t; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); done(); } catch (e) { /* nothing more to try */ } a.remove(); }

  /* ------------------------------------------------------------------ market scan */
  const STEPS = `<div class="steps"><div class="step"><b>1</b><div><h3>Get prices</h3>Press <i>Sync from NSE</i> or <i>Import file</i> for real bhavcopy, or switch Prices to the synthetic demo to try the screens.</div></div>
<div class="step"><b>2</b><div><h3>Press Find trades</h3>No stock needed. Every F&amp;O stock is scanned, then the most promising are evaluated in full on trend, Greeks and open interest.</div></div>
<div class="step"><b>3</b><div><h3>Read the ideas</h3>Each shows what it risks and can make. Open one for the legs, payoff chart and exit plan. Staying out is a valid answer too.</div></div></div>`;

  function setMode(m, skipExpiryLoad) {
    S.mode = m; S.view = m === "market" ? "scan" : "stock";
    $("#fnoModeMkt").classList.toggle("on", m === "market"); $("#fnoModeOne").classList.toggle("on", m === "one");
    $("#fnoModeMkt").setAttribute("aria-pressed", m === "market"); $("#fnoModeOne").setAttribute("aria-pressed", m === "one");
    $("#fnoSymWrap").style.display = m === "one" ? "" : "none";
    $("#fnoExpiryWrap").style.display = (m === "market" ? S.source === "bhavcopy" : !!val("#fnoSym").trim()) ? "" : "none";
    $("#fnoRunSug").textContent = m === "market" ? "Find trades" : "Suggest trades";
    $("#fnoRunBt").style.display = m === "one" && S.source !== "breeze-live" ? "" : "none";
    $("#fnoModeMkt").disabled = S.source === "breeze-live";
    $("#fnoModeHint").textContent = S.source === "breeze-live"
      ? "Breeze-live evaluates one stock using the current option chain and recent 5-minute data."
      : m === "market" ? "Scans every F&O stock and ranks the best trades by Greeks and open interest. No stock needed." : "Looks at one stock in detail: its trend, IV, open interest and the best trades.";
    if (S.status && !skipExpiryLoad) loadExpiryOptions();
    renderMain();
  }
  const renderMain = () => S.view === "scan" ? renderScan() : renderSug();
  const runPrimary = () => S.mode === "market" ? runScan() : runSuggest();
  function openStock(sym, backtest) { setMode("one"); $("#fnoSym").value = sym; if (backtest) runBacktest(); else { setTab("sug"); runSuggest(); } }

  async function runScan() {
    if (S.source === "breeze-live") { say("#fnoMsg", "Breeze-live supports one stock at a time; use Suggest trades.", "err"); return; }
    const expiry = S.source === "bhavcopy" ? val("#fnoExpiry") : "";
    if (S.source === "bhavcopy" && !expiry) { say("#fnoMsg", "Select an available option expiry first.", "err"); return; }
    const b = $("#fnoRunSug"); b.disabled = true; setTab("sug"); S.view = "scan";
    say("#fnoMsg", busy("Scanning stocks for the selected expiry (10 to 30 seconds)…"));
    const req = Object.assign(params(), { top_n: 10 });
    if (expiry) req.expiry = expiry;
    try { S.scan = await post("/scan", req); say("#fnoMsg", ""); renderScan(); }
    catch (e) { say("#fnoMsg", esc(e.message), "err"); } finally { b.disabled = false; }
  }

  function marketStrip(r) {
    const m = r.market || {}; if (!m.stocks) return "";
    const t = (m.bullish + m.bearish + m.neutral) || 1, w = n => (n / t * 100).toFixed(1);
    const skippedN = Object.values(r.skipped || {}).reduce((a, b) => a + b, 0);
    const ivr = m.median_iv_rank;
    return `<div class="strip">
<div class="tile"><div class="l"><span>Stocks scanned</span></div><div class="v">${m.stocks}</div><div class="s">of ${r.universe} with F&amp;O that day${skippedN ? " · " + skippedN + " skipped" : ""}</div></div>
<div class="tile"><div class="l"><span>Trend mix</span></div><div class="mix" title="bullish / range-bound / bearish"><i class="b" style="width:${w(m.bullish)}%"></i><i class="n" style="width:${w(m.neutral)}%"></i><i class="r" style="width:${w(m.bearish)}%"></i></div>
  <div class="s"><span class="pos">${m.bullish} bullish</span> · ${m.neutral} range · <span class="neg">${m.bearish} bearish</span></div></div>
<div class="tile"><div class="l"><span>Median IV rank</span></div><div class="v">${ivr == null ? "n/a" : ivr.toFixed(0)}</div><div class="s">${ivr == null ? "needs more history" : ivr >= 50 ? "option premium is rich across the market" : ivr <= 30 ? "option premium is cheap across the market" : "middling"}</div></div>
<div class="tile"><div class="l"><span>Put/call OI (median)</span></div><div class="v">${m.median_pcr == null ? "n/a" : m.median_pcr.toFixed(2)}</div><div class="s">${m.above_sma20_pct}% of stocks above their 20-day average</div></div>
<div class="tile"><div class="l"><span>Futures build-up</span></div><div class="v"><span class="pos">${m.long_buildup || 0}</span> long · <span class="neg">${m.short_buildup || 0}</span> short</div><div class="s">${m.short_covering || 0} covering · ${m.long_unwinding || 0} unwinding</div></div></div>`;
  }

  function filterRow(ideas) {
    const B = [["all", "All"], ["bullish", "Bullish"], ["bearish", "Bearish"], ["neutral", "Range-bound"]], K = [["all", "Any type"], ["credit", "Selling premium"], ["debit", "Buying premium"], ["futures", "Futures"]];
    const chip = (attr, val, label, cur, n) => `<button type="button" class="fchip ${cur === val ? "on" : ""}" ${attr}="${val}">${label}${n != null ? ` <small>${n}</small>` : ""}</button>`;
    return `<div class="fchips"><span class="mute">Show</span>${B.map(([v, l]) => chip("data-fb", v, l, S.filter.bias, v === "all" ? ideas.length : countWhere(ideas, "bias", v))).join("")}<span class="sep"></span>${K.map(([v, l]) => chip("data-fk", v, l, S.filter.kind, v === "all" ? null : countWhere(ideas, "kind", v))).join("")}</div>`;
  }

  function renderScan() {
    const r = S.scan, el = $("#fnoPanSug");
    if (!r) { el.innerHTML = STEPS; return; }
    let h = `<h2 style="font-size:15px;margin:0 0 4px">Market scan <span class="mute" style="font-weight:400">as of ${r.asof ? esc(fdate(r.asof)) : "—"}${r.expiry ? ` · expiry ${esc(fdate(r.expiry))}` : ""}${r.eligible != null ? ` · ${r.eligible} stocks looked at, ${r.shortlisted} evaluated in full` : ""}</span></h2>`;
    const notes = r.data_notes || [];
    const notesHtml = r.source === "synthetic" ? notes.map(n => `<div class="note bad">${esc(n)}</div>`).join("")
      : notes.length ? `<details><summary>About this data (${notes.length} note${notes.length > 1 ? "s" : ""})</summary>${notes.map(n => `<div class="note info">${esc(n)}</div>`).join("")}</details>` : "";
    if (r.issue) { el.innerHTML = h + issuePanel(r.issue) + notesHtml; wireFix(el, runScan); return; }
    h += notesHtml + marketStrip(r);
    const ideas = r.ideas || [], shown = filterIdeas(ideas, S.filter);
    if (!ideas.length) {
      h += `<div class="panel"><h3>No trade found</h3><div style="font-size:15px;margin-bottom:6px">${esc(r.note || "Nothing passed the filters.")}</div>
<div class="hint">Staying out is a valid answer. A trade is only suggested when the trend, IV level, Greeks, open interest and costs agree.</div>
${(r.top_shortlist || []).length ? `<div class="sec-title">Stocks that came closest in the first screen</div><div class="scroll"><table class="compact"><thead><tr><th>Stock</th><th>View</th><th class="r">Shortlist score</th></tr></thead><tbody>${r.top_shortlist.map(x => `<tr><td><b>${esc(x.symbol)}</b></td><td>${esc(x.bias)}</td><td class="r">${x.score}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`;
    } else {
      h += filterRow(ideas) + `<div class="hint" style="margin:0 0 10px">Showing ${shown.length} of ${ideas.length} ideas, best first. One idea per stock. Margin is not calculated: check your broker's margin calculator before trading.</div>`;
      h += shown.length ? shown.map((x, i) => suggestionCard(x, i, { scan: true, rank: x.scan.rank })).join("") : `<div class="empty" style="padding:24px">No idea matches this filter. Try "All".</div>`;
    }
    const sk = Object.entries(r.skipped || {});
    if (sk.length) h += `<details><summary>${sk.reduce((a, [, n]) => a + n, 0)} stocks were skipped</summary><ul class="why">${sk.map(([k, n]) => `<li><b>${n}</b> with ${esc(k)}</li>`).join("")}</ul><div class="hint">Skipped means the data could not support a judgement: they were not scored.</div></details>`;
    el.innerHTML = h; hookCharts(el);
    el.querySelectorAll("[data-fb]").forEach(b => b.addEventListener("click", () => { S.filter.bias = b.dataset.fb; renderScan(); }));
    el.querySelectorAll("[data-fk]").forEach(b => b.addEventListener("click", () => { S.filter.kind = b.dataset.fk; renderScan(); }));
    el.querySelectorAll(".sug").forEach(card => wireCard(card, shown[+card.dataset.i]));
  }

  /* ------------------------------------------------------------------ backtest */
  async function runBacktest() {
    if (S.source === "breeze-live") { say("#fnoMsg", "Backtests require historical option-chain snapshots; Breeze-live is for current single-stock ideas only.", "err"); return; }
    const p = params(); if (!p.symbol) { say("#fnoMsg", "Enter a symbol.", "err"); return; }
    const b = $("#fnoRunBt"); b.disabled = true; setTab("bt"); say("#fnoMsg", busy("Backtesting the rules and a random-trend baseline… this can take 10 to 30 seconds."));
    try { S.bt = await post("/backtest", p); say("#fnoMsg", ""); renderBacktest(); } catch (e) { say("#fnoMsg", esc(e.message), "err"); } finally { b.disabled = false; }
  }

  function cmpRow(label, k, fmt, a, b) { return `<tr><td>${label}</td><td class="r">${a ? fmt(a[k]) : "—"}</td><td class="r">${b ? fmt(b[k]) : "—"}</td></tr>`; }

  function attribution(m) {
    const a = m.attribution || {}, rows = [["delta", "Delta (direction)"], ["gamma", "Gamma (big moves)"], ["theta", "Theta (time)"], ["vega", "Vega (IV change)"], ["entry_slippage", "Entry slippage"], ["exit_slippage", "Exit slippage"], ["unexplained", "Unexplained"]];
    const mx = Math.max.apply(null, rows.map(([k]) => Math.abs(a[k] || 0)).concat([1]));
    return `<div class="attr">${rows.map(([k, l]) => { const v = a[k] || 0, w = Math.abs(v) / mx * 50;
      return `<div class="a"><span>${l}</span><span class="t"><b></b><i style="${v >= 0 ? "left:50%" : "right:50%"};width:${w}%;background:${v >= 0 ? "var(--f-good)" : "var(--f-bad)"}"></i></span><span class="r" style="text-align:right">${sinr(v)}</span></div>`; }).join("")}</div>
<div class="hint">Where the rupees came from, summed over all closed trades. Costs (taxes, brokerage) are separate and shown above.</div>`;
  }

  function renderBacktest() {
    const r = S.bt, el = $("#fnoPanBt");
    if (!r) { el.innerHTML = `<div class="empty">Press <b>Run backtest</b> to replay the rules over the stored history,<br>next to a baseline that picks the trend at random.</div>`; return; }
    const m = r.signal.metrics, rm = r.random && r.random.metrics;
    if (r.issue) { el.innerHTML = `<h2 style="font-size:15px;margin:0 0 8px">${esc(r.symbol)} backtest</h2>` + issuePanel(r.issue) + (r.data_notes || []).map(n => `<div class="note info">${esc(n)}</div>`).join(""); wireFix(el, runBacktest); return; }
    let h = `<h2 style="font-size:15px;margin:0 0 8px">${esc(r.symbol)} backtest <span class="mute" style="font-weight:400">${r.bars} days of prices · ${esc(r.source === "bhavcopy" ? "NSE bhavcopy" : "synthetic")}</span></h2>`;
    h += (r.data_notes || []).map(n => `<div class="note ${r.source === "synthetic" ? "bad" : ""}">${esc(n)}</div>`).join("");
    if (!m.trades) { el.innerHTML = h + `<div class="empty">The rules found no trades in this history.${r.signal.skipped ? "<br>" + Object.entries(r.signal.skipped).slice(0, 3).map(([k, v]) => `${esc(k)} (${v} days)`).join("<br>") : ""}</div>`; return; }
    h += `<div class="cards">${[["Closed trades", m.trades], ["Win rate", m.win_rate + "%"], ["Expectancy", sgn(m.expectancy_r, 2) + " R"], ["Net P&amp;L", sinr(m.total_net_pnl)], ["Profit factor", m.profit_factor == null ? "—" : m.profit_factor], ["Max drawdown", inr(m.max_drawdown)], ["Costs paid", inr(m.total_costs)], ["Avg days held", m.avg_days_held]]
      .map(([l, v]) => `<div class="tile"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("")}</div>`;
    if (r.edge && r.edge.verdict) h += `<div class="note ${r.edge.difference_r > 0.05 ? "info" : ""}"><b>Signal vs random trend:</b> ${esc(r.edge.verdict)}${r.edge.difference_r != null ? ` (${sgn(r.edge.difference_r, 2)} R per trade; ${r.edge.signal_trades} signal trades vs ${r.edge.random_trades} random).` : ""}</div>`;
    const sp = r.signal.equity.map(p => parseDay(p.date).getTime()), rp = r.random ? r.random.equity : [];
    if (sp.length > 1) {
      const xs = sp.slice(), series = [{ name: "Selector signals", ys: r.signal.equity.map(p => p.pnl), color: "#2f704b", width: 2.4 }];
      const all = sp.concat(rp.map(p => parseDay(p.date).getTime())).sort((a, b) => a - b), grid = Array.from(new Set(all));
      const at = (pts, t) => { let v = 0; pts.forEach(p => { if (parseDay(p.date).getTime() <= t) v = p.pnl; }); return v; };
      const sig = grid.map(t => at(r.signal.equity, t)), rnd = rp.length ? grid.map(t => at(rp, t)) : null;
      const ser = [{ name: "Selector signals", ys: sig, color: "#2f704b", width: 2.4 }].concat(rnd ? [{ name: "Random trend (first run)", ys: rnd, color: "#a17820", dash: "5 4", width: 1.8 }] : []);
      h += `<div class="panel"><h3>Cumulative net P&amp;L by exit date</h3>${lineChart({ xs: grid, series: ser, zero: true, height: 260, xfmt: v => new Date(v).toLocaleDateString("en-IN", { month: "short", year: "2-digit" }), xtip: v => new Date(v).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }), aria: "Cumulative profit and loss" })}</div>`;
    }
    h += `<div class="panel"><h3>Selector against the random-trend baseline</h3><div class="scroll"><table class="compact"><thead><tr><th></th><th class="r">Selector signals</th><th class="r">Random trend${r.random ? " (" + r.random.runs + " runs pooled)" : ""}</th></tr></thead><tbody>
${cmpRow("Closed trades", "trades", v => v, m, rm)}${cmpRow("Win rate", "win_rate", v => v == null ? "—" : v + "%", m, rm)}${cmpRow("Expectancy (R)", "expectancy_r", v => v == null ? "—" : sgn(v, 2), m, rm)}
${cmpRow("Net P&amp;L", "total_net_pnl", sinr, m, rm)}${cmpRow("Profit factor", "profit_factor", v => v == null ? "—" : v, m, rm)}${cmpRow("Max drawdown", "max_drawdown", inr, m, rm)}</tbody></table></div>
<div class="hint">R is one unit of planned risk (the stop distance). The baseline uses the same structures, strikes, exits and costs, with the trend drawn at random. If the signal does not clearly beat it, the technical view adds nothing here.</div></div>`;
    h += `<div class="panel"><h3>By structure</h3><div class="scroll"><table class="compact"><thead><tr><th>Structure</th><th class="r">Trades</th><th class="r">Win rate</th><th class="r">Avg R</th><th class="r">Net P&amp;L</th></tr></thead><tbody>${
      Object.entries(m.by_strategy).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r">${v.trades}</td><td class="r">${v.win_rate}%</td><td class="r">${sgn(v.avg_r, 2)}</td><td class="r">${sinr(v.net_pnl)}</td></tr>`).join("")}</tbody></table></div></div>`;
    h += `<div class="panel"><h3>Where the P&amp;L came from</h3>${attribution(m)}</div>`;
    const c = m.calibration;
    if (c && c.buckets) h += `<div class="panel"><h3>Was the model's win chance right?</h3><div class="scroll"><table class="compact"><thead><tr><th>Predicted</th><th class="r">Trades</th><th class="r">Predicted win rate</th><th class="r">Actual win rate</th></tr></thead><tbody>${
      Object.entries(c.buckets).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r">${v.trades}</td><td class="r">${v.predicted}%</td><td class="r">${v.realised}%</td></tr>`).join("")}</tbody></table></div>
<div class="hint">Brier score ${c.brier} (lower is better; 0.25 is a coin flip). The model chance assumes holding to expiry while trades exit early on rules, so this is indicative only.</div></div>`;
    h += `<details><summary>${r.signal.trades.length} most recent trades</summary><div class="scroll"><table class="compact"><thead><tr><th>Entered</th><th>Structure</th><th>Exited</th><th>Why</th><th class="r">Days</th><th class="r">Net</th><th class="r">R</th></tr></thead><tbody>${
      r.signal.trades.slice().reverse().map(t => `<tr><td>${sdate(t.entry_date)}</td><td>${esc(t.strategy)}</td><td>${t.exit_date ? sdate(t.exit_date) : "open"}</td><td>${esc(t.exit_reason)}</td><td class="r">${t.days_held}</td><td class="r">${sinr(t.net_pnl)}</td><td class="r">${sgn(t.r_multiple, 2)}</td></tr>`).join("")}</tbody></table></div></details>`;
    el.innerHTML = h; hookCharts(el);
  }

  /* ------------------------------------------------------------------ my log */
  function addToLog(s) {
    if (S.log.some(e => sameKey(e, s))) return;
    S.log.push(slim(s));
    if (!saveLog()) say("#fnoMsg", "This browser isn't keeping the log (private window or storage full). Export a copy.", "err");
    renderLog();
  }

  async function refreshOutcomes() {
    const b = $("#fnoLogRefresh"); if (!S.log.length) return; b.disabled = true; say("#fnoLogMsg", busy("Replaying your suggestions over the data since…"));
    try {
      const r = await post("/evaluate", { entries: S.log.slice(-300) });
      S.out = {}; r.rows.forEach(x => { S.out[x.id] = x; }); S.outSummary = r;
      renderLog();                                                     // re-renders the panel, so the message goes in AFTER it
      say("#fnoLogMsg", "Updated " + new Date().toLocaleTimeString("en-IN") + ". Each suggestion is assumed filled at its suggested prices.");
    } catch (e) { say("#fnoLogMsg", esc(e.message), "err"); b.disabled = false; }
  }

  function renderLog() {
    const el = $("#fnoPanLog"); $("#fnoLogN").textContent = S.log.length;
    if (!S.log.length) { el.innerHTML = `<div class="empty">Your log is empty.<br>Press <b>Add to my log</b> on a suggestion. They are kept in this browser, and you can check how each one played out later.</div>`; return; }
    const rows = S.log.slice().reverse().map(e => {
      const o = S.out[e.id], st = o ? STATUS[o.status] || [o.status, "mute"] : null, plan = e.exit_plan;
      return `<tr><td>${sdate(e.asof)}</td><td><b>${esc(e.symbol)}</b></td><td>${esc(e.structure.name)}</td><td class="r">${e.risk.net_premium ? (e.risk.net_premium > 0 ? "cr " : "db ") + inr(Math.abs(e.risk.net_premium)) : "—"}</td>
<td class="r">${sinr(plan.profit_target)} / ${sinr(plan.stop_loss)}</td><td>${st ? `<span class="badge ${st[1]}" ${o.error ? `title="${esc(o.error)}"` : ""}>${st[0]}</span>` : '<span class="mute">not checked</span>'}</td>
<td class="r">${o && o.status !== "no_data" ? sinr(o.net_pnl) : "—"}</td><td class="r">${o && o.days_held != null ? o.days_held + "d" : "—"}</td>
<td><input type="checkbox" data-taken="${esc(e.id)}" ${e.taken ? "checked" : ""} aria-label="I took this trade" /></td><td><button class="small" data-rm="${esc(e.id)}" aria-label="Remove">✕</button></td></tr>`;
    }).join("");
    let sum = "";
    if (S.outSummary) {
      const A = S.outSummary.all, T = S.outSummary.taken, line = (l, x) => `<tr><td>${l}</td><td class="r">${x.trades || 0}</td><td class="r">${x.win_rate != null ? x.win_rate + "%" : "—"}</td><td class="r">${x.expectancy_r != null ? sgn(x.expectancy_r, 2) + " R" : "—"}</td><td class="r">${x.total_net_pnl != null ? sinr(x.total_net_pnl) : "—"}</td></tr>`;
      sum = `<div class="panel"><h3>How your suggestions played out</h3><div class="scroll"><table class="compact"><thead><tr><th></th><th class="r">Finished</th><th class="r">Win rate</th><th class="r">Expectancy</th><th class="r">Net P&amp;L</th></tr></thead><tbody>${line("All suggestions", A)}${T.trades ? line("The ones you took", T) : ""}</tbody></table></div>
<div class="hint">${S.outSummary.open_positions} still open (${sinr(S.outSummary.open_net_pnl)} so far)${S.outSummary.no_data ? ` · ${S.outSummary.no_data} could not be checked (no data)` : ""}. ${A.small_sample ? `Only ${A.trades || 0} finished: far too few to judge the rules.` : ""}</div></div>`;
    }
    el.innerHTML = `<div class="btns" style="margin:0 0 12px"><button class="small" id="fnoLogRefresh">Refresh outcomes</button><button class="small" id="fnoLogExport">Export JSON</button><button class="small" id="fnoLogImport">Import JSON…</button><input type="file" id="fnoLogFile" accept=".json" hidden /></div>
<div class="status" id="fnoLogMsg"></div>${sum}<div class="panel" style="padding:8px 10px"><div class="scroll"><table class="compact"><thead><tr><th>Date</th><th>Stock</th><th>Structure</th><th class="r">Premium</th><th class="r">Target / stop</th><th>Status</th><th class="r">Net P&amp;L</th><th class="r">Held</th><th>Took</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>
<div class="hint">Kept in this browser, not on the server. Clearing site data erases it: export a copy.</div>`;
    $("#fnoLogRefresh").addEventListener("click", refreshOutcomes);
    $("#fnoLogExport").addEventListener("click", exportLog);
    $("#fnoLogImport").addEventListener("click", () => $("#fnoLogFile").click());
    $("#fnoLogFile").addEventListener("change", ev => importLog(ev.target.files[0]));
    el.querySelectorAll("[data-taken]").forEach(c => c.addEventListener("change", () => { const e = S.log.find(x => x.id === c.dataset.taken); if (e) { e.taken = c.checked; saveLog(); } }));
    el.querySelectorAll("[data-rm]").forEach(b => b.addEventListener("click", () => { S.log = S.log.filter(x => x.id !== b.dataset.rm); delete S.out[b.dataset.rm]; saveLog(); renderLog(); renderSug(); }));
  }

  function exportLog() {
    const blob = new Blob([JSON.stringify({ app: "nse-fno", kind: "suggestion-log", version: 1, saved_at: new Date().toISOString(), entries: S.log }, null, 1)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "fno-log-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  async function importLog(file) {
    if (!file) return;
    try {
      const d = JSON.parse(await file.text()), list = Array.isArray(d) ? d : d.entries;
      if (!Array.isArray(list)) throw new Error("That file doesn't look like an F&O log.");
      let added = 0;
      list.forEach(e => { if (e && e.id && e.symbol && e.structure && e.exit_plan && !S.log.some(x => sameKey(x, e))) { S.log.push(e); added++; } });
      saveLog(); renderLog(); say("#fnoLogMsg", `${added} added, ${list.length - added} skipped (already there or not valid).`);
    } catch (e) { say("#fnoLogMsg", esc(e.message || "Couldn't read that file."), "err"); }
  }

  /* ------------------------------------------------------------------ mount */
  function mount(host, scope) {
    S.host = host; S.scope = scope || "local";
    if (!document.getElementById("fno-style")) { const st = document.createElement("style"); st.id = "fno-style"; st.textContent = CSS; document.head.appendChild(st); }
    host.innerHTML = shell();
    $("#fnoSource").addEventListener("change", async e => {
      S.source = e.target.value; S.sug = null; S.scan = null; S.liveQuote = null;
      if (S.source === "breeze-live") { $("#fnoAsof").value = ""; setMode("one", true); setTab("sug"); }
      renderStatus(); await loadSymbols();
    });
    $("#fnoSync").addEventListener("click", () => doSync());
    $("#fnoSyncLatest").addEventListener("click", () => doSync({}));
    $("#fnoStop").addEventListener("click", () => { S.syncStop = true; say("#fnoDataMsg", busy("Stopping after this batch…")); });
    $("#fnoImport").addEventListener("click", () => $("#fnoFile").click());
    $("#fnoFile").addEventListener("change", e => doImport(e.target.files[0]));
    $("#fnoRunSug").addEventListener("click", runPrimary);
    $("#fnoModeMkt").addEventListener("click", () => setMode("market"));
    $("#fnoModeOne").addEventListener("click", () => setMode("one"));
    $("#fnoRunBt").addEventListener("click", runBacktest);
    $("#fnoSym").addEventListener("change", () => {
      S.liveQuote = null;
      $("#fnoLiveQuote").textContent = S.source === "breeze-live"
        ? "Refresh the ICICI Breeze quote or suggest trades from live 5-minute data."
        : "ICICI Breeze quote is separate from strategy inputs, which use the selected market-data source.";
      loadExpiryOptions();
    });
    $("#fnoLiveRefresh").addEventListener("click", refreshLiveQuote);
    $("#fnoBreezeTest").addEventListener("click", testBreezeConnection);
    ["#fnoBreezeApiKey", "#fnoBreezeApiSecret", "#fnoBreezeSessionToken"].forEach(sel => $(sel).addEventListener("input", () => {
      S.breezeConnection = "not tested";
      const status = $("#fnoBreezeTestStatus");
      status.textContent = "Credentials changed; test again.";
      status.className = "status";
    }));
    $("#fnoChatToggle").addEventListener("click", () => {
      const panel = $("#fnoChatPanel"), open = panel.hidden;
      panel.hidden = !open;
      $("#fnoChatToggle").setAttribute("aria-expanded", String(open));
      if (open) { renderChatHistory(); $("#fnoChatMessage").focus(); }
    });
    $("#fnoChatClose").addEventListener("click", () => {
      $("#fnoChatPanel").hidden = true;
      $("#fnoChatToggle").setAttribute("aria-expanded", "false");
      $("#fnoChatToggle").focus();
    });
    updateChatProvider();
    $("#fnoChatMode").addEventListener("change", updateChatMode);
    $("#fnoChatProvider").addEventListener("change", updateChatProvider);
    $("#fnoChatForm").addEventListener("submit", sendChat);
    $("#fnoSym").addEventListener("keydown", e => { if (e.key === "Enter") { loadExpiryOptions().then(runSuggest); } });
    $("#fnoAsof").addEventListener("change", loadExpiryOptions);
    S.host.querySelectorAll(".fno-tabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.t)));
    loadLog(); setMode("market"); renderBacktest(); renderLog(); loadStatus();
    root.FnoUI = { state: S, runSuggest, runBacktest };
  }

  async function boot() {
    let scope = "local";
    try { const r = await fetch("/api/auth/status"); if (r.ok) { const d = await r.json(); if (d.user && d.user.email) scope = d.user.email; } } catch (e) { /* standalone: no sign-in */ }
    const standalone = document.getElementById("fno-root");
    if (standalone) return mount(standalone, scope);
    const tabs = document.querySelector(".topbar .tabs");
    if (!tabs) return;                                   // neither host: nothing to do
    const tab = document.createElement("div"); tab.className = "tab"; tab.dataset.view = "fno"; tab.textContent = "F&O";
    const settings = tabs.querySelector('[data-view="settings"]'); tabs.insertBefore(tab, settings || null);
    const sec = document.createElement("section"); sec.className = "view"; sec.id = "view-fno";
    document.body.insertBefore(sec, document.querySelector("script")); mount(sec, scope);
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(t => t.classList.remove("active")); document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
      tab.classList.add("active"); sec.classList.add("active");
    });
  }

  const api_ = { esc, inr, sinr, pct, sgn, fdate, sdate, niceTicks, compact, lineChart, clamp, plainWords, filterIdeas, countWhere, answerAppChat };
  if (typeof module !== "undefined" && module.exports) module.exports = api_;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
  }
})(typeof window !== "undefined" ? window : globalThis);
