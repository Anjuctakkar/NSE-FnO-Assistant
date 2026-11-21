// Unit tests for the UI's pure helpers (run by test_ui.py with node).
const u = require("../fno/static/fno.js");
let bad = 0;
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) { console.log("FAIL", m, "\n  got ", JSON.stringify(a), "\n  want", JSON.stringify(b)); bad = 1; } };
eq(u.esc("<a href=\"x\">&'</a>"), "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;", "esc escapes all five characters");
eq(u.esc(null), "", "esc(null)");
eq(u.inr(-12345.6), "−₹12,346", "negative rupees");
eq(u.inr(1234567), "₹12,34,567", "Indian digit grouping");
eq(u.inr(null), "—", "null rupees");
eq(u.inr(Infinity), "—", "infinite rupees show a dash, not 'Infinity'");
eq(u.pct(0.1234, 1), "12.3%", "pct");
eq(u.sgn(-0.5, 1), "−0.5", "true minus sign");
eq(u.niceTicks(0, 100, 5), [0, 20, 40, 60, 80, 100], "ticks");
eq(u.niceTicks(-3700, 9100, 5).includes(0), true, "ticks include zero when it is in range");
eq(u.niceTicks(5, 5, 5), [5], "degenerate range");
eq(u.compact(1500000), "15L", "lakh");
eq(u.compact(-2500), "−2.5k", "thousand");
const h = u.lineChart({ xs: [0, 1, 2, 3], series: [{ name: "a", ys: [-5, 0, 5, 10], color: "#fff" }], zero: true, fill: 0, marks: [{ x: 1, label: "BE" }] });
eq([/<svg/.test(h), /area-up/.test(h), /class="zero"/.test(h), />BE</.test(h)], [true, true, true, true], "chart draws svg, shading, zero line, label");
eq(u.lineChart({ xs: [0, 1], series: [{ name: "<img onerror=x>", ys: [1, 2], color: "#fff" }, { name: "b", ys: [2, 3], color: "#000" }] }).includes("<img onerror"), false, "legend names are escaped");
eq(u.lineChart({ xs: [0, 1, 2], series: [{ name: "flat", ys: [5, 5, 5], color: "#fff" }] }).includes("NaN"), false, "a flat series does not produce NaN coordinates");
const mk = (kind, delta, theta, vega, be) => ({ kind, greeks: { delta, theta, vega }, risk: { breakevens: be } });
eq(u.plainWords(mk("credit", 300, 420, -900, [1832.86])), ["Makes money if the stock is above ₹1,833 at expiry.", "Time is on your side: roughly ₹420 a day of decay works in your favour.", "Gains if volatility falls, loses if it rises (for example after a big move)."], "credit spread wording");
eq(u.plainWords(mk("debit", -1250, -45, 649, [1832.86]))[0], "Makes money if the stock is below ₹1,833 at expiry.", "bearish: below breakeven");
eq(u.plainWords(mk("credit", 5, 300, -500, [1700, 1900]))[0], "Makes money if the stock stays between ₹1,700 and ₹1,900 at expiry.", "condor: a range");
eq(u.plainWords(mk("futures", 250, 0, 0, [100]))[0].startsWith("Moves one-for-one"), true, "futures wording");
const ideas = [{ kind: "credit", scan: { bias: "bullish" } }, { kind: "debit", scan: { bias: "bullish" } }, { kind: "credit", scan: { bias: "bearish" } }, { kind: "futures", scan: { bias: "neutral" } }, { kind: "debit" }];
eq(u.filterIdeas(ideas, { bias: "all", kind: "all" }).length, 5, "no filter keeps everything");
eq(u.filterIdeas(ideas, { bias: "bullish", kind: "all" }).length, 2, "bias filter");
eq(u.filterIdeas(ideas, { bias: "all", kind: "credit" }).length, 2, "type filter");
eq(u.filterIdeas(ideas, { bias: "bullish", kind: "credit" }).length, 1, "both filters combine");
eq(u.filterIdeas(ideas, { bias: "bearish", kind: "futures" }).length, 0, "no match gives an empty list");
eq(u.filterIdeas(ideas, { bias: "bullish", kind: "debit" })[0].scan.bias, "bullish", "an idea with no scan block never matches a bias filter");
eq([u.countWhere(ideas, "bias", "bullish"), u.countWhere(ideas, "kind", "credit"), u.countWhere(ideas, "bias", "neutral")], [2, 2, 1], "chip counts");
eq(u.answerAppChat("Is Breeze connected?", { connection: "connected" }).includes("test in this page succeeded"), true, "chat reports the last Breeze connection test");
eq(u.answerAppChat("What is the current quote?", { quote: { symbol: "TCS", ltp: 102.5, change: 2.5, change_pct: 0.025, timestamp: "10:15" } }).includes("LTP ₹102.5"), true, "chat reads the loaded quote");
const chatIdea = {
  symbol: "TCS", source: "bhavcopy", asof: "2026-11-20", score: 82, kind: "debit",
  structure: { name: "Bull Call Spread", legs: [{ expiry: "2026-12-31" }] },
  legs_detail: [{ side: 1, kind: "CE", strike: 100, price: 5 }],
  risk: { max_loss: 500 }, exit_plan: { profit_target: 200, stop_loss: -100 },
  reasons: ["Trend agrees", "Costs fit"]
};
const chatIdeaAnswer = u.answerAppChat("Explain my current trade idea", {
  view: "stock", stock: { symbol: "TCS", source: "bhavcopy", asof: "2026-11-20", suggestions: [chatIdea] }
});
eq([chatIdeaAnswer.includes("Bull Call Spread"), chatIdeaAnswer.includes("BUY 100 call"), chatIdeaAnswer.includes("maximum loss: ₹500")],
  [true, true, true], "chat summarizes the currently displayed strategy and risk");
eq(u.answerAppChat("Show the RSI value", {
  view: "stock", stock: { symbol: "TCS", regime: { rsi: 68 } }
}).includes("RSI for TCS is 68"), true, "chat explains a displayed indicator value");
eq(u.answerAppChat("What should I ask?", {}).includes("Try asking"), true, "chat gives supported prompt examples");
eq(u.answerAppChat("How do I run a market scan?", {}).includes("Market scan"), true, "chat explains the UI workflow");
eq(u.answerAppChat("Where do I enter my Breeze settings?", {}).includes("Settings"), true, "chat explains the settings UI");
eq(u.answerAppChat("Is Breeze connected?", { connection: "connected" }).includes("Confidence: High"), true, "chat labels direct answers with brief confidence");
const scoreExample = u.answerAppChat("Can you give a score example?", {});
eq([scoreExample.includes("25 + IV fit 20"), scoreExample.includes("75/100"), scoreExample.includes("not a win probability"),
  scoreExample.includes("separate first-pass ranking")], [true, true, true, true], "chat gives a brief, distinct scan and trade score example");
process.exit(bad);
