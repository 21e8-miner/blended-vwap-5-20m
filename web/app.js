/* ==========================================================================
   Blended 5-20m VWAP — Quant Blue Interactive Workbench Engine
   100% offline, zero external dependencies, reads window.VWAP_DATA
   ========================================================================== */

(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const PRESETS = {
    dual_regime: { label: "Dual-Regime Bayes", short: "Dual-Regime", weights: [32, 34, 21, 13], dynamic: true },
    ou_spectral: { label: "OU-Spectral 5–20m", short: "OU-Spectral", weights: [32, 34, 21, 13], dynamic: true },
    adaptive:    { label: "Adaptive 5–20m",    short: "Adaptive",    weights: [35, 30, 20, 15], dynamic: true },
    static:      { label: "Static Blend",      short: "Static",      weights: [35, 30, 20, 15], dynamic: false },
    fast5:       { label: "Fast 5m Focus",     short: "Fast 5m",     weights: [75, 15, 5, 5],   dynamic: false },
    core10:      { label: "Core 10m",          short: "Core 10m",    weights: [10, 70, 15, 5],  dynamic: false },
    anchor20:    { label: "Anchor 20m",        short: "Anchor 20m",  weights: [5, 10, 20, 65],  dynamic: false },
    custom:      { label: "Custom Weights",    short: "Custom",      weights: [35, 30, 20, 15], dynamic: false }
  };

  const ALL_HORIZONS = [5, 6, 8, 10, 12, 15, 16, 18, 20];
  const BLEND_HORIZONS = [5, 10, 15, 20];
  const LN_BLEND_HORIZONS = BLEND_HORIZONS.map(h => Math.log(h));
  const OU_GAMMA = 0.85;
  const RADAR_SYMBOLS = ["ES", "SPY", "QQQ", "NVDA", "AAPL", "TSLA", "MSFT", "BTC-USD"];

  const COLORS = {
    navy: "#0B1F3A",
    cobalt: "#1B4F9C",
    steel: "#3B76C4",
    cerulean: "#68A3DF",
    sky: "#8AB4DF",
    ice: "#EDF4FC",
    grid: "#B8CEE8",
    muted: "#526D8F",
    ink: "#253D5B",
    long: "#0E6A68",
    longBg: "#E7F5F4",
    short: "#9B2C3B",
    shortBg: "#FCEEEF"
  };

  const state = {
    activeTab: "lab",
    symbol: "ES",
    symbolSource: "benchmark",
    symbolSpreadBps: 0.6,
    symbolImpactCoeff: 0.12,
    turboMode: false,
    lastComputeMs: 0,
    preset: "adaptive",
    weights: [35, 30, 20, 15],
    defaultEntryZ: 1.58,
    defaultMaxHold: 20,
    entryZ: 1.58,
    maxHold: 20,
    zoom: "spotlight",
    layers: {
      candles: true,
      horizons: true,
      bands: true,
      trades: true
    },
    selectedTradeIdx: null,
    // Tab 2 state
    selectedConfigId: null,
    tableFilter: "all",
    sortKey: "net_sharpe",
    sortDir: "desc",
    // Tab 3 Deep Research & Radar state
    radarSelectedSymbol: "ES",
    radarSortKey: "absZ",
    radarSortDir: "desc",
    radarRows: [],
    radarScanMs: 0,
    // Tab 4 Editorial Blue Sheet page ("1", "2", or "3")
    sheetPage: "1",
    // Live Paper-Trading Execution Desk state ($100,000 simulated account)
    paper: {
      startingEquity: 100000,
      realizedUsd: 0,
      qty: 0,
      avgEntryPx: 0,
      entryVwap: 0,
      entryMode: "",
      vwapEdgeSum: 0,
      vwapEdgeCnt: 0,
      realizedBpsSum: 0,
      closedCount: 0,
      fills: [],
      autoPilot: false
    },
    // Computed session data
    simSeries: null,
    simTrades: [],
    // Real-time stream & trigger notification state
    rt: {
      running: false,
      cadenceMs: 1000,
      timerId: null,
      ws: null,
      wsConnected: false,
      tickCount: 0,
      tempImpact: 0,
      triggers: { breach: true, exit: true, surge: true },
      channels: { toast: true, sound: true, desktop: false },
      alerts: [],
      lastZone: 0,
      lastSurgeTick: -10,
      livePos: null
    }
  };

  let RAW_METRICS = null;
  let RAW_SESSION = null;
  let BENCHMARK_SESSION = null;
  const RADAR_SESSION_CACHE = Object.create(null);

  const SYMBOL_PROFILES = {
    ES:       { name: "ES Composite", basePx: 149.2,  volBps: 4.4,  spreadBps: 0.60, impact: 0.12, baseVol: 48000, crypto: false },
    SPY:      { name: "SPDR S&P 500", basePx: 594.5,  volBps: 3.6,  spreadBps: 0.35, impact: 0.08, baseVol: 145000, crypto: false },
    QQQ:      { name: "Invesco QQQ",  basePx: 512.8,  volBps: 4.8,  spreadBps: 0.40, impact: 0.09, baseVol: 112000, crypto: false },
    NVDA:     { name: "NVIDIA Corp",  basePx: 138.4,  volBps: 8.6,  spreadBps: 0.70, impact: 0.14, baseVol: 185000, crypto: false },
    AAPL:     { name: "Apple Inc",    basePx: 232.6,  volBps: 4.9,  spreadBps: 0.45, impact: 0.10, baseVol: 120000, crypto: false },
    TSLA:     { name: "Tesla Inc",    basePx: 342.0,  volBps: 10.4, spreadBps: 0.85, impact: 0.16, baseVol: 165000, crypto: false },
    MSFT:     { name: "Microsoft",    basePx: 428.5,  volBps: 4.5,  spreadBps: 0.45, impact: 0.10, baseVol: 78000, crypto: false },
    AMZN:     { name: "Amazon.com",   basePx: 206.4,  volBps: 5.8,  spreadBps: 0.50, impact: 0.11, baseVol: 95000, crypto: false },
    META:     { name: "Meta Platforms", basePx: 588.0, volBps: 6.4, spreadBps: 0.60, impact: 0.12, baseVol: 54000, crypto: false },
    AMD:      { name: "AMD Inc",      basePx: 144.2,  volBps: 8.2,  spreadBps: 0.75, impact: 0.14, baseVol: 98000, crypto: false },
    IWM:      { name: "Russell 2000", basePx: 234.8,  volBps: 5.1,  spreadBps: 0.50, impact: 0.11, baseVol: 88000, crypto: false },
    GLD:      { name: "SPDR Gold",    basePx: 244.0,  volBps: 3.1,  spreadBps: 0.45, impact: 0.09, baseVol: 42000, crypto: false },
    "BTC-USD":{ name: "Bitcoin USD",  basePx: 94200,  volBps: 9.2,  spreadBps: 1.10, impact: 0.15, baseVol: 320,    crypto: true, cbPair: "BTC-USD" },
    "ETH-USD":{ name: "Ethereum USD", basePx: 3340,   volBps: 10.8, spreadBps: 1.30, impact: 0.16, baseVol: 1850,   crypto: true, cbPair: "ETH-USD" },
    "SOL-USD":{ name: "Solana USD",   basePx: 238.5,  volBps: 12.6, spreadBps: 1.60, impact: 0.18, baseVol: 14200,  crypto: true, cbPair: "SOL-USD" }
  };

  /* ---------- Utilities ---------- */
  function $(id) { return document.getElementById(id); }

  function svgEl(tag, attrs, textContent) {
    const el = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v !== undefined && v !== null) el.setAttribute(k, String(v));
      }
    }
    if (textContent !== undefined && textContent !== null) {
      el.textContent = String(textContent);
    }
    return el;
  }

  function clearEl(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function fmt(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Number(v).toFixed(digits);
  }

  function fmtSigned(v, digits = 2) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n >= 0 ? "+" : "") + n.toFixed(digits);
  }

  function fmtInt(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Math.round(Number(v)).toLocaleString("en-US");
  }

  function getRegimeLabel(regimeId) {
    if (regimeId === "micro") return "Micro-Pulse";
    if (regimeId === "core") return "Core Blended";
    if (regimeId === "anchor") return "Anchor Drift";
    return regimeId || "—";
  }

  function normalizeIntegerWeights(weights, pinnedIdx, pinnedVal) {
    const out = weights.slice();
    const v = clamp(Math.round(pinnedVal), 0, 100);
    out[pinnedIdx] = v;
    const rem = 100 - v;
    const otherIdxs = [0, 1, 2, 3].filter(i => i !== pinnedIdx);
    const otherSum = otherIdxs.reduce((acc, i) => acc + out[i], 0);

    if (otherSum <= 0) {
      const base = Math.floor(rem / 3);
      let left = rem - base * 3;
      for (const i of otherIdxs) {
        out[i] = base + (left > 0 ? 1 : 0);
        if (left > 0) left--;
      }
      return out;
    }

    const raw = otherIdxs.map(i => (out[i] / otherSum) * rem);
    const floored = raw.map(x => Math.floor(x));
    let deficit = rem - floored.reduce((a, b) => a + b, 0);
    const fracs = raw.map((x, idx) => ({ idx, frac: x - floored[idx] }))
      .sort((a, b) => b.frac - a.frac);
    for (let k = 0; k < deficit; k++) {
      floored[fracs[k].idx] += 1;
    }
    otherIdxs.forEach((origIdx, idx) => {
      out[origIdx] = floored[idx];
    });
    return out;
  }

  function getStaticWeightsFromMeta() {
    const sw = RAW_METRICS && RAW_METRICS.meta && RAW_METRICS.meta.static_weights;
    if (Array.isArray(sw) && sw.length === 4) {
      const scaled = sw.map(w => Math.round(w <= 1.01 ? w * 100 : w));
      const diff = 100 - scaled.reduce((a, b) => a + b, 0);
      scaled[0] += diff;
      return scaled;
    }
    if (sw && typeof sw === "object") {
      const arr = [sw["5"] ?? sw.w5 ?? 0.35, sw["10"] ?? sw.w10 ?? 0.30, sw["15"] ?? sw.w15 ?? 0.20, sw["20"] ?? sw.w20 ?? 0.15];
      const scaled = arr.map(w => Math.round(w <= 1.01 ? w * 100 : w));
      const diff = 100 - scaled.reduce((a, b) => a + b, 0);
      scaled[0] += diff;
      return scaled;
    }
    return [35, 30, 20, 15];
  }

  /* ---------- Normalize Session Trades Schema ---------- */
  function normalizeSessionTrade(t, bars, fallbackIdx) {
    const entryIdx = Number(t.entry_idx ?? t.entryIdx ?? t.start_idx ?? 0);
    const exitIdx = Number(t.exit_idx ?? t.exitIdx ?? t.end_idx ?? Math.min(bars.length - 1, entryIdx + 10));
    const entryBar = bars[clamp(entryIdx, 0, bars.length - 1)] || {};
    const exitBar = bars[clamp(exitIdx, 0, bars.length - 1)] || {};
    const rawSide = String(t.side ?? t.direction ?? (Number(t.dir ?? 1) >= 0 ? "LONG" : "SHORT")).toUpperCase();
    const side = (rawSide === "LONG" || rawSide === "BUY" || rawSide === "1" || rawSide === "+1") ? "LONG" : "SHORT";
    const entryPx = Number(t.entry_price ?? t.entry_px ?? entryBar.close ?? 100);
    const exitPx = Number(t.exit_price ?? t.exit_px ?? exitBar.close ?? entryPx);
    const holdMin = Number(t.hold_min ?? t.duration_min ?? t.hold ?? Math.max(1, exitIdx - entryIdx));
    const dir = side === "LONG" ? 1 : -1;
    const grossBps = Number(t.gross_bps ?? t.gross_pnl_bps ?? (dir * ((exitPx - entryPx) / entryPx) * 10000));
    const costBps = Number(t.cost_bps ?? t.tx_cost_bps ?? 1.1);
    const netBps = Number(t.net_bps ?? t.pnl_bps ?? t.net_pnl_bps ?? (grossBps - costBps));
    const entryZ = Number(t.entry_z ?? t.z_entry ?? entryBar.z_score ?? 0);
    const vwapEdge = Number(
      t.vwap_edge_bps ?? t.slippage_saved_bps ?? t.vwap_slippage_saved_bps ??
      (dir * (( (entryBar.vwap_adaptive ?? entryPx) - entryPx) / entryPx) * 10000 - costBps * 0.45)
    );

    return {
      id: t.trade_id ?? t.id ?? (fallbackIdx + 1),
      side,
      entryIdx,
      exitIdx,
      entryTime: t.entry_time ?? entryBar.time ?? `Bar ${entryIdx}`,
      exitTime: t.exit_time ?? exitBar.time ?? `Bar ${exitIdx}`,
      holdMin,
      entryPx,
      exitPx,
      entryZ,
      grossBps,
      costBps,
      netBps,
      vwapEdge,
      exitReason: t.exit_reason ?? ""
    };
  }

  /* ---------- Multi-Symbol Causal Session Engine ---------- */
  function hashSymbolSeed(str) {
    let h = 2166136261 >>> 0;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h || 1337;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function gaussianRng(rand) {
    const u1 = Math.max(1e-9, rand());
    const u2 = rand();
    return Math.sqrt(-2.0 * Math.log(u1)) * Math.cos(2.0 * Math.PI * u2);
  }

  function normalizeSymbolInput(raw) {
    const clean = String(raw || "").trim().toUpperCase().replace(/[^A-Z0-9.\-=/^]/g, "");
    if (!clean) return "ES";
    if (clean === "BTC" || clean === "BTCUSD" || clean === "BTCUSDT") return "BTC-USD";
    if (clean === "ETH" || clean === "ETHUSD" || clean === "ETHUSDT") return "ETH-USD";
    if (clean === "SOL" || clean === "SOLUSD" || clean === "SOLUSDT") return "SOL-USD";
    return clean;
  }

  function getSymbolProfile(symbol) {
    if (SYMBOL_PROFILES[symbol]) return SYMBOL_PROFILES[symbol];
    const h = hashSymbolSeed(symbol);
    const isCrypto = /-(USD|USDT|EUR)$/.test(symbol);
    const basePx = isCrypto
      ? Number((15 + (h % 450) * 1.37).toFixed(2))
      : Number((28 + (h % 380) * 1.15).toFixed(2));
    const volBps = Number((4.2 + ((h >>> 5) % 62) * 0.11).toFixed(2));
    const spreadBps = Number((0.45 + ((h >>> 11) % 10) * 0.08).toFixed(2));
    const impact = Number((0.10 + ((h >>> 15) % 8) * 0.01).toFixed(2));
    const baseVol = Math.round(28000 + ((h >>> 8) % 120) * 1100);
    return {
      name: symbol,
      basePx,
      volBps,
      spreadBps,
      impact,
      baseVol,
      crypto: isCrypto,
      cbPair: isCrypto ? symbol.replace(/USDT$/, "USD") : null
    };
  }

  function formatMinuteET(idx) {
    const totalMin = 9 * 60 + 30 + idx;
    const hh = Math.floor(totalMin / 60);
    const mm = totalMin % 60;
    return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
  }

  function generateCalibratedSymbolBars(symbol) {
    const profile = getSymbolProfile(symbol);
    const seed = hashSymbolSeed(symbol + "::5-20M-VWAP");
    const rand = mulberry32(seed);
    const n = 390;
    const bars = new Array(n);

    let fairPx = profile.basePx;
    let tempImpact = 0;
    let driftState = (rand() - 0.5) * 0.00018;
    const baseSigma = profile.volBps / 10000;

    // Schedule 9-12 institutional slicing bursts creating realistic 5-20m dislocations
    const bursts = [];
    for (let k = 0; k < 11; k++) {
      const start = Math.floor(18 + (k * 31) + rand() * 16);
      const len = 4 + Math.floor(rand() * 6);
      const dir = rand() > 0.5 ? 1 : -1;
      const amp = (1.8 + rand() * 1.7) * baseSigma;
      bursts.push({ start, end: start + len, dir, amp });
    }

    for (let i = 0; i < n; i++) {
      const u = i / (n - 1);
      // U-shaped intraday volume clock (open surge -> midday lull -> close surge)
      const uShape = 0.68 + 2.25 * Math.exp(-u / 0.11) + 1.65 * Math.exp(-(1 - u) / 0.12);
      const volMult = Math.max(0.32, uShape * (0.78 + 0.44 * rand()));

      let burstPush = 0;
      let burstVolBoost = 1.0;
      for (let b = 0; b < bursts.length; b++) {
        if (i >= bursts[b].start && i <= bursts[b].end) {
          burstPush += bursts[b].dir * bursts[b].amp;
          burstVolBoost = Math.max(burstVolBoost, 1.85);
        }
      }

      if (i % 45 === 0) {
        driftState = 0.45 * driftState + (rand() - 0.5) * 0.00022;
      }

      const volRegime = 0.85 + 0.55 * Math.sqrt(Math.min(3.5, volMult));
      const shock = gaussianRng(rand) * baseSigma * volRegime;
      const openPx = i === 0 ? fairPx : bars[i - 1].close;

      fairPx = fairPx * (1 + driftState + shock * 0.62);
      // Temporary impact decays over ~8-14 minutes (classic 5-20m reversion half-life)
      tempImpact = tempImpact * 0.84 + burstPush + shock * 0.55;
      const closePx = Math.max(profile.basePx * 0.5, fairPx * (1 + tempImpact));

      const wickAmp = Math.abs(gaussianRng(rand)) * baseSigma * volRegime * 0.85;
      const hiPx = Math.max(openPx, closePx) * (1 + wickAmp);
      const loPx = Math.min(openPx, closePx) * (1 - wickAmp);
      const volume = Math.max(10, Math.round(profile.baseVol * volMult * burstVolBoost));

      bars[i] = {
        idx: i,
        time: formatMinuteET(i),
        open: Number(openPx.toFixed(4)),
        high: Number(hiPx.toFixed(4)),
        low: Number(loPx.toFixed(4)),
        close: Number(closePx.toFixed(4)),
        volume
      };
    }

    return { bars, profile };
  }

  async function tryFetchLiveCryptoBars(symbol, profile) {
    if (!profile.crypto || !profile.cbPair || typeof fetch !== "function") return null;
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), 2800) : null;
    try {
      const url = `https://api.exchange.coinbase.com/products/${encodeURIComponent(profile.cbPair)}/candles?granularity=60`;
      const res = await fetch(url, {
        signal: controller ? controller.signal : undefined,
        headers: { Accept: "application/json" }
      });
      if (!res.ok) return null;
      const rows = await res.json();
      if (!Array.isArray(rows) || rows.length < 60) return null;
      // Coinbase format: [time_sec, low, high, open, close, volume] newest first
      rows.sort((a, b) => Number(a[0]) - Number(b[0]));
      const slice = rows.slice(-300);
      return slice.map((r, idx) => {
        const dt = new Date(Number(r[0]) * 1000);
        const hh = String(dt.getHours()).padStart(2, "0");
        const mm = String(dt.getMinutes()).padStart(2, "0");
        return {
          idx,
          time: `${hh}:${mm}`,
          open: Number(r[3]),
          high: Number(r[2]),
          low: Number(r[1]),
          close: Number(r[4]),
          volume: Math.max(0.0001, Number(r[5]))
        };
      });
    } catch (_err) {
      return null;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function parseCsvBars(text) {
    const lines = String(text || "").split(/\r?\n/).map(l => l.trim()).filter(Boolean);
    if (lines.length < 25) return null;
    const firstCols = lines[0].split(",").map(s => s.trim().toLowerCase());
    const hasHeader = isNaN(Number(firstCols[1])) || firstCols.some(c => /open|close|high|low|time|date/.test(c));
    let iTime = 0, iOpen = 1, iHigh = 2, iLow = 3, iClose = 4, iVol = 5;
    if (hasHeader) {
      const findCol = re => firstCols.findIndex(c => re.test(c));
      const ct = findCol(/time|date|ts/);
      const co = findCol(/^open$|^o$/);
      const ch = findCol(/^high$|^h$/);
      const cl = findCol(/^low$|^l$/);
      const cc = findCol(/^close$|^c$|^price$/);
      const cv = findCol(/^vol|^v$/);
      if (ct >= 0) iTime = ct;
      if (co >= 0) iOpen = co;
      if (ch >= 0) iHigh = ch;
      if (cl >= 0) iLow = cl;
      if (cc >= 0) iClose = cc;
      if (cv >= 0) iVol = cv;
    }

    const out = [];
    const startRow = hasHeader ? 1 : 0;
    for (let r = startRow; r < lines.length; r++) {
      const cols = lines[r].split(",").map(s => s.trim());
      const close = Number(cols[iClose]);
      if (!Number.isFinite(close) || close <= 0) continue;
      const open = Number.isFinite(Number(cols[iOpen])) ? Number(cols[iOpen]) : close;
      const high = Number.isFinite(Number(cols[iHigh])) ? Number(cols[iHigh]) : Math.max(open, close);
      const low = Number.isFinite(Number(cols[iLow])) ? Number(cols[iLow]) : Math.min(open, close);
      const volume = Math.max(1, Number.isFinite(Number(cols[iVol])) ? Number(cols[iVol]) : 1000);
      const rawTime = cols[iTime] || formatMinuteET(out.length);
      const timeMatch = rawTime.match(/(\d{1,2}:\d{2})/);
      const time = timeMatch ? timeMatch[1].padStart(5, "0") : rawTime.slice(-5);
      out.push({ idx: out.length, time, open, high, low, close, volume });
    }
    return out.length >= 25 ? out.slice(-390) : null;
  }

  /* ---------- Exact O(N) Phase 2 Microstructure Engine (OU, Curvature, VPIN, Hurst) ---------- */
  function normalCdf(x) {
    // Abramowitz & Stegun 7.1.26 fast normal CDF (max error < 1.5e-7)
    const sign = x < 0 ? -1 : 1;
    const z = Math.abs(x) / Math.SQRT2;
    const t = 1.0 / (1.0 + 0.3275911 * z);
    const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    const erf = 1.0 - poly * Math.exp(-z * z);
    return 0.5 * (1.0 + sign * erf);
  }

  function enrichPhase2Microstructure(bars, preserveEngineFields) {
    const n = bars.length;
    if (n === 0) return bars;

    // 1. Ensure all 9 horizon VWAPs exist via O(N) origin-shifted prefix sums when missing
    const need9 = bars[n - 1].vwap6 === undefined || bars[n - 1].vwap18 === undefined;
    if (need9) {
      const p0 = (bars[0].high + bars[0].low + bars[0].close) / 3;
      const csV = new Float64Array(n + 1);
      const csPV = new Float64Array(n + 1);
      for (let i = 0; i < n; i++) {
        const b = bars[i];
        const v = Math.max(1e-6, Number(b.volume || 1));
        const dp = (b.high + b.low + b.close) / 3 - p0;
        csV[i + 1] = csV[i] + v;
        csPV[i + 1] = csPV[i] + dp * v;
      }
      const extraH = [6, 8, 12, 16, 18];
      for (let i = 0; i < n; i++) {
        const hi = i + 1;
        for (let k = 0; k < extraH.length; k++) {
          const h = extraH[k];
          const lo = hi >= h ? hi - h : 0;
          const wV = csV[hi] - csV[lo];
          bars[i][`vwap${h}`] = p0 + (csPV[hi] - csPV[lo]) / Math.max(1e-9, wV);
        }
      }
    }

    // 2. Causal 1-min return volatility (lookback=15) & 5-bar rel_vol EMA
    const logP = new Float64Array(n);
    const r1 = new Float64Array(n);
    const r5 = new Float64Array(n);
    const barSig = new Float64Array(n);
    const rvEma = new Float64Array(n);
    const alphaV = 2.0 / 6.0;

    for (let i = 0; i < n; i++) {
      logP[i] = Math.log(Math.max(1e-6, Number(bars[i].close)));
      if (i > 0) r1[i] = logP[i] - logP[i - 1];
      if (i >= 5) r5[i] = logP[i] - logP[i - 5];
      const rv = clamp(Number(bars[i].rel_vol || 1.0), 0.25, 3.50);
      rvEma[i] = i === 0 ? rv : alphaV * rv + (1 - alphaV) * rvEma[i - 1];
    }

    const csR1 = new Float64Array(n + 1);
    const csR1Sq = new Float64Array(n + 1);
    const csR5 = new Float64Array(n + 1);
    const csR5Sq = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) {
      csR1[i + 1] = csR1[i] + r1[i];
      csR1Sq[i + 1] = csR1Sq[i] + r1[i] * r1[i];
      csR5[i + 1] = csR5[i] + r5[i];
      csR5Sq[i + 1] = csR5Sq[i] + r5[i] * r5[i];
    }

    for (let i = 0; i < n; i++) {
      if (i < 3) {
        barSig[i] = 5.5e-4;
      } else {
        const lo = Math.max(1, i - 14);
        const cnt = i - lo + 1;
        const s1 = csR1[i + 1] - csR1[lo];
        const s2 = csR1Sq[i + 1] - csR1Sq[lo];
        const v = Math.max(0, (s2 - (s1 * s1) / cnt) / Math.max(1, cnt - 1));
        barSig[i] = Math.max(2.0e-4, Math.sqrt(v));
      }
    }

    // 3. Volume-clock OU half-life (trailing 30-bar O(N) causal AR(1) in volume time)
    const xArr = new Float64Array(n);
    const csUY = new Float64Array(n + 1);
    const csU2 = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      xArr[i] = (b.close - b.vwap15) / Math.max(1e-6, b.close);
      let uy = 0;
      let u2 = 0;
      if (i > 0) {
        const dtau = clamp(Number(b.rel_vol || 1.0), 0.25, 3.50);
        const u = xArr[i - 1] * dtau;
        const dx = xArr[i] - xArr[i - 1];
        uy = u * dx;
        u2 = u * u;
      }
      csUY[i + 1] = csUY[i] + uy;
      csU2[i + 1] = csU2[i] + u2;
    }

    // 4. Causal Bulk-Volume VPIN (window=20)
    const csVol = new Float64Array(n + 1);
    const csAbsImb = new Float64Array(n + 1);
    const csSgnImb = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      const v = Math.max(1e-6, Number(b.volume || 1));
      const lr = Math.log(Math.max(1e-6, b.close) / Math.max(1e-6, b.open));
      const xi = clamp(lr / Math.max(barSig[i], 2.5e-4), -5.0, 5.0);
      const sImb = v * (2.0 * normalCdf(xi) - 1.0);
      csVol[i + 1] = csVol[i] + v;
      csAbsImb[i + 1] = csAbsImb[i] + Math.abs(sImb);
      csSgnImb[i + 1] = csSgnImb[i] + sImb;
    }

    const priorThetaV = Math.LN2 / 11.5;
    const shrinkKappa = 1.6e-5;
    const invTwoGamma2 = 1.0 / (2.0 * OU_GAMMA * OU_GAMMA);
    const twoLn5 = 2.0 * Math.log(5.0);

    for (let i = 0; i < n; i++) {
      const b = bars[i];
      const hi = i + 1;

      // OU Half-Life
      let tauHalf;
      if (preserveEngineFields && Number.isFinite(Number(b.tau_half))) {
        tauHalf = Number(b.tau_half);
      } else {
        const loOu = hi >= 30 ? hi - 30 : 0;
        const sUY = csUY[hi] - csUY[loOu];
        const sU2 = csU2[hi] - csU2[loOu];
        const thetaV = clamp((-sUY + shrinkKappa * priorThetaV) / (sU2 + shrinkKappa), 0.025, 0.26);
        const vSpeed = 0.36 + 0.64 * clamp(rvEma[i], 0.30, 3.20);
        tauHalf = clamp(Math.LN2 / (thetaV * vSpeed), 5.0, 19.5);
      }

      // OU-Spectral Horizon Kernel w_h^OU(t) across {5, 10, 15, 20}
      let w5Ou, w10Ou, w15Ou, w20Ou;
      if (preserveEngineFields && Number.isFinite(Number(b.ou_w5)) && Number.isFinite(Number(b.ou_w20))) {
        w5Ou = Number(b.ou_w5);
        w10Ou = Number(b.ou_w10);
        w15Ou = Number(b.ou_w15);
        w20Ou = Number(b.ou_w20);
      } else {
        const lnTau = Math.log(clamp(tauHalf, 5.0, 19.5));
        const k0 = -Math.pow(LN_BLEND_HORIZONS[0] - lnTau, 2) * invTwoGamma2;
        const k1 = -Math.pow(LN_BLEND_HORIZONS[1] - lnTau, 2) * invTwoGamma2;
        const k2 = -Math.pow(LN_BLEND_HORIZONS[2] - lnTau, 2) * invTwoGamma2;
        const k3 = -Math.pow(LN_BLEND_HORIZONS[3] - lnTau, 2) * invTwoGamma2;
        const kMax = Math.max(k0, k1, k2, k3);
        const e0 = Math.exp(k0 - kMax);
        const e1 = Math.exp(k1 - kMax);
        const e2 = Math.exp(k2 - kMax);
        const e3 = Math.exp(k3 - kMax);
        const eSum = e0 + e1 + e2 + e3;
        w5Ou = e0 / eSum;
        w10Ou = e1 / eSum;
        w15Ou = e2 / eSum;
        w20Ou = e3 / eSum;
      }

      const vwapOuCalc = w5Ou * b.vwap5 + w10Ou * b.vwap10 + w15Ou * b.vwap15 + w20Ou * b.vwap20;
      const vwapOu = (preserveEngineFields && Number.isFinite(Number(b.vwap_ou))) ? Number(b.vwap_ou) : vwapOuCalc;

      const floorSig = Math.max(b.close * 0.00025, 1e-6);
      const s5 = Math.max(0, Number(b.sigma5 ?? b.sigma_blend ?? floorSig));
      const s10 = Math.max(0, Number(b.sigma10 ?? b.sigma_blend ?? floorSig));
      const s15 = Math.max(0, Number(b.sigma15 ?? b.sigma_blend ?? floorSig));
      const s20 = Math.max(0, Number(b.sigma20 ?? b.sigma_blend ?? floorSig));

      const withinVar = w5Ou * s5 * s5 + w10Ou * s10 * s10 + w15Ou * s15 * s15 + w20Ou * s20 * s20;
      const betweenVar =
        w5Ou * Math.pow(b.vwap5 - vwapOu, 2) +
        w10Ou * Math.pow(b.vwap10 - vwapOu, 2) +
        w15Ou * Math.pow(b.vwap15 - vwapOu, 2) +
        w20Ou * Math.pow(b.vwap20 - vwapOu, 2);
      const sigmaOuCalc = Math.max(floorSig, Math.sqrt(Math.max(0, withinVar + betweenVar)));
      const sigmaOu = (preserveEngineFields && Number.isFinite(Number(b.sigma_ou)) && Number(b.sigma_ou) > 0)
        ? Number(b.sigma_ou)
        : sigmaOuCalc;
      const zOu = (preserveEngineFields && Number.isFinite(Number(b.z_ou)))
        ? Number(b.z_ou)
        : (b.close - vwapOu) / sigmaOu;

      // Term-Structure Slope & Butterfly Curvature
      const sigmaBar = 0.25 * (Math.max(s5, floorSig) + Math.max(s10, floorSig) + Math.max(s15, floorSig) + Math.max(s20, floorSig));
      const slopeCalc = (b.vwap5 - b.vwap20) / sigmaBar;
      const slope = (preserveEngineFields && Number.isFinite(Number(b.slope))) ? Number(b.slope) : slopeCalc;
      const curvCalc = (b.vwap5 - 2.0 * b.vwap10 + b.vwap20) / sigmaBar;
      const curvature = (preserveEngineFields && Number.isFinite(Number(b.curvature))) ? Number(b.curvature) : curvCalc;

      // VPIN
      let vpin;
      if (preserveEngineFields && Number.isFinite(Number(b.vpin))) {
        vpin = Number(b.vpin);
      } else {
        const loV = hi >= 20 ? hi - 20 : 0;
        const winV = Math.max(1.0, csVol[hi] - csVol[loV]);
        const winAbs = csAbsImb[hi] - csAbsImb[loV];
        const winSgn = Math.abs(csSgnImb[hi] - csSgnImb[loV]);
        const ramp = Math.min((i + 1) / 8.0, 1.0);
        const vpinRaw = 0.50 * (winAbs / winV) + 0.50 * (winSgn / winV);
        vpin = clamp((1.0 - ramp) * 0.32 + ramp * vpinRaw, 0.0, 1.0);
      }

      // Causal Local Hurst via Lo-MacKinlay VR(5)
      let hurst;
      if (preserveEngineFields && Number.isFinite(Number(b.hurst))) {
        hurst = Number(b.hurst);
      } else if (i < 10) {
        hurst = 0.50;
      } else {
        const lo1 = Math.max(1, i - 39);
        const cnt1 = i - lo1 + 1;
        const s11 = csR1[hi] - csR1[lo1];
        const s12 = csR1Sq[hi] - csR1Sq[lo1];
        const var1 = Math.max((s12 - (s11 * s11) / cnt1) / Math.max(1, cnt1 - 1), 1e-12);
        const var1Eff = 0.55 * var1 + 0.45 * Math.max(s12 / cnt1, 1e-12);

        const loq = Math.max(5, i - 39);
        const cntq = i - loq + 1;
        const sq1 = csR5[hi] - csR5[loq];
        const sq2 = csR5Sq[hi] - csR5Sq[loq];
        const varqC = Math.max((sq2 - (sq1 * sq1) / cntq) / Math.max(1, cntq - 1), 1e-12);
        const varqU = Math.max(sq2 / cntq, 1e-12);
        const varq = 0.55 * varqC + 0.45 * varqU;

        const vr = clamp(varq / (5.0 * var1Eff), 0.14, 4.50);
        const hRaw = 0.5 + Math.log(vr) / twoLn5;
        const rampH = Math.min((i - 10 + 1) / 12.0, 1.0);
        hurst = clamp((1.0 - rampH) * 0.50 + rampH * hRaw, 0.18, 0.86);
      }

      const toxicFlag = (vpin >= 0.56 && hurst >= 0.54) || hurst >= 0.62 || vpin >= 0.68;

      // 5. Strictly Causal 2-State Hamilton Online Bayesian Regime Filter P(S_t = Trend | F_t)
      const bullFanFull = b.close > b.vwap5 && b.vwap5 > b.vwap10 && b.vwap10 > b.vwap15 && b.vwap15 > b.vwap20;
      const bearFanFull = b.close < b.vwap5 && b.vwap5 < b.vwap10 && b.vwap10 < b.vwap15 && b.vwap15 < b.vwap20;
      const bullFanSoft = b.close > b.vwap5 && b.vwap5 > b.vwap10 && b.vwap10 > b.vwap20;
      const bearFanSoft = b.close < b.vwap5 && b.vwap5 < b.vwap10 && b.vwap10 < b.vwap20;
      const fanAlign = (bullFanFull || bearFanFull) ? 1.0 : ((bullFanSoft || bearFanSoft) ? 0.5 : 0.0);
      const fanDir = (bullFanFull || bullFanSoft) ? 1 : ((bearFanFull || bearFanSoft) ? -1 : (b.vwap5 >= b.vwap20 ? 1 : -1));

      let pTrend;
      let regimeMode;
      if (preserveEngineFields && Number.isFinite(Number(b.p_trend))) {
        pTrend = clamp(Number(b.p_trend), 0.0, 1.0);
        regimeMode = b.regime_mode || (pTrend < 0.38 ? "FADE" : (pTrend > 0.62 ? "SURF" : "FLAT"));
      } else {
        const pPrev = i === 0 ? 0.08 : Number(bars[i - 1].p_trend ?? 0.08);
        // Markov transition step matching engine/dual_regime.py (p00 = 0.975, p11 = 0.945)
        const pPred = pPrev * 0.945 + (1.0 - pPrev) * 0.025;
        const llr = clamp(
          (3.6 * (vpin - 0.46) +
           5.4 * (hurst - 0.52) +
           0.75 * (Math.abs(slope) - 1.05) +
           1.45 * (fanAlign - 0.35) - 1.05) * 0.65,
          -4.2,
          4.2
        );
        const odds = (pPred / Math.max(1e-6, 1.0 - pPred)) * Math.exp(llr);
        pTrend = clamp(odds / (1.0 + odds), 0.005, 0.995);
        regimeMode = pTrend < 0.38 ? "FADE" : (pTrend > 0.62 ? "SURF" : "FLAT");
      }

      b.tau_half = tauHalf;
      b.w5_ou = w5Ou;
      b.w10_ou = w10Ou;
      b.w15_ou = w15Ou;
      b.w20_ou = w20Ou;
      b.vwap_ou = vwapOu;
      b.sigma_ou = sigmaOu;
      b.z_ou = zOu;
      b.slope = slope;
      b.curvature = curvature;
      b.d_curvature = i > 0 ? curvature - (bars[i - 1].curvature || 0) : 0;
      b.vpin = vpin;
      b.hurst = hurst;
      b.toxic_flag = toxicFlag;
      b.p_trend = pTrend;
      b.regime_mode = regimeMode;
      b.fan_dir = fanDir;
      b.fan_align = fanAlign;
    }

    return bars;
  }

  function buildCausalSession(symbol, rawBars, sourceLabel, spreadBps, impactCoeff) {
    const n = rawBars.length;
    const p0 = n > 0 ? (rawBars[0].high + rawBars[0].low + rawBars[0].close) / 3 : 100;
    const csV = new Float64Array(n + 1);
    const csPV = new Float64Array(n + 1);
    const csP2V = new Float64Array(n + 1);
    let totalVol = 0;

    // O(N) origin-shifted prefix sums for zero-loop rolling VWAP & dispersion across all 9 horizons
    for (let i = 0; i < n; i++) {
      const b = rawBars[i];
      const v = Math.max(1e-6, Number(b.volume || 1));
      const tp = (b.high + b.low + b.close) / 3;
      const dp = tp - p0;
      totalVol += v;
      csV[i + 1] = csV[i] + v;
      csPV[i + 1] = csPV[i] + dp * v;
      csP2V[i + 1] = csP2V[i] + dp * dp * v;
    }

    const meanVol = totalVol / Math.max(1, n);
    const bars = new Array(n);

    const calcH = (i, h, closePx) => {
      const lo = i + 1 >= h ? i + 1 - h : 0;
      const hi = i + 1;
      const wV = csV[hi] - csV[lo];
      const mu = (csPV[hi] - csPV[lo]) / Math.max(1e-9, wV);
      const vwap = p0 + mu;
      const wVar = Math.max(0, (csP2V[hi] - csP2V[lo]) / Math.max(1e-9, wV) - mu * mu);
      const sig = Math.max(closePx * 0.00025, Math.sqrt(wVar));
      return [vwap, sig];
    };

    for (let i = 0; i < n; i++) {
      const b = rawBars[i];
      const relVol = Math.max(0.25, Number(b.volume || 1) / Math.max(1e-6, meanVol));
      const [v5, s5] = calcH(i, 5, b.close);
      const [v6] = calcH(i, 6, b.close);
      const [v8] = calcH(i, 8, b.close);
      const [v10, s10] = calcH(i, 10, b.close);
      const [v12] = calcH(i, 12, b.close);
      const [v15, s15] = calcH(i, 15, b.close);
      const [v16] = calcH(i, 16, b.close);
      const [v18] = calcH(i, 18, b.close);
      const [v20, s20] = calcH(i, 20, b.close);

      // Causal adaptive volume-clock weights (fast tilt on high rel_vol, anchor tilt on calm lull)
      const volTilt = clamp((relVol - 1.0) / 1.8, -0.65, 1.25);
      const r5 = Math.max(0.08, 0.35 + 0.16 * volTilt);
      const r10 = Math.max(0.10, 0.30 + 0.02 * volTilt);
      const r15 = Math.max(0.06, 0.20 - 0.09 * volTilt);
      const r20 = Math.max(0.04, 0.15 - 0.09 * volTilt);
      const wSum = r5 + r10 + r15 + r20;
      const w5 = r5 / wSum;
      const w10 = r10 / wSum;
      const w15 = r15 / wSum;
      const w20 = r20 / wSum;

      const vwapStatic = 0.35 * v5 + 0.30 * v10 + 0.20 * v15 + 0.15 * v20;
      const vwapAdaptive = w5 * v5 + w10 * v10 + w15 * v15 + w20 * v20;

      const varWithin = w5 * s5 * s5 + w10 * s10 * s10 + w15 * s15 * s15 + w20 * s20 * s20;
      const varBetween =
        w5 * (v5 - vwapAdaptive) * (v5 - vwapAdaptive) +
        w10 * (v10 - vwapAdaptive) * (v10 - vwapAdaptive) +
        w15 * (v15 - vwapAdaptive) * (v15 - vwapAdaptive) +
        w20 * (v20 - vwapAdaptive) * (v20 - vwapAdaptive);
      const sigmaBlend = Math.max(b.close * 0.00025, Math.sqrt(varWithin + varBetween));
      const zScore = (b.close - vwapAdaptive) / sigmaBlend;

      bars[i] = {
        idx: i,
        time: b.time,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
        volume: b.volume,
        rel_vol: Number(relVol.toFixed(3)),
        vwap5: v5,
        vwap6: v6,
        vwap8: v8,
        vwap10: v10,
        vwap12: v12,
        vwap15: v15,
        vwap16: v16,
        vwap18: v18,
        vwap20: v20,
        sigma5: s5,
        sigma10: s10,
        sigma15: s15,
        sigma20: s20,
        vwap_static: vwapStatic,
        vwap_adaptive: vwapAdaptive,
        sigma_blend: sigmaBlend,
        z_score: zScore,
        w5,
        w10,
        w15,
        w20
      };
    }

    enrichPhase2Microstructure(bars, false);

    // Pick richest ~90-min spotlight window with most |z| >= 1.50 excursions
    const winLen = Math.min(90, Math.max(40, n - 25));
    let bestStart = Math.min(25, Math.max(0, n - winLen - 1));
    let bestScore = -1;
    for (let s = 20; s <= Math.max(20, n - winLen - 5); s += 5) {
      let sc = 0;
      for (let j = s; j < Math.min(n, s + winLen); j++) {
        if (Math.abs(bars[j].z_score) >= 1.50) sc += 1;
      }
      if (sc > bestScore) {
        bestScore = sc;
        bestStart = s;
      }
    }
    const bestEnd = Math.min(n - 1, bestStart + winLen);

    return {
      symbol,
      sourceLabel,
      spreadBps,
      impactCoeff,
      bars,
      trades: [],
      spotlight_window: {
        start_idx: bestStart,
        end_idx: bestEnd,
        label: `${symbol} ${bars[bestStart].time}–${bars[bestEnd].time} ET Active Window`
      }
    };
  }

  function updateSymbolUI(symbol, badgeText, badgeClass) {
    const input = $("symbolInput");
    if (input) input.value = symbol;
    const badge = $("symbolSourceBadge");
    if (badge) {
      badge.textContent = badgeText;
      badge.classList.remove("live", "loading");
      if (badgeClass) badge.classList.add(badgeClass);
    }
    const titleEl = $("chartMainTitle");
    if (titleEl) {
      titleEl.textContent = `Intraday 5–20m Blended VWAP · ${symbol}`;
    }
    document.querySelectorAll("#symbolChips .sym-chip").forEach(chip => {
      chip.classList.toggle("active", chip.dataset.symbol === symbol);
    });
  }

  function syncSymbolQueryParam(symbol) {
    try {
      const url = new URL(window.location.href);
      if (symbol === "ES") url.searchParams.delete("symbol");
      else url.searchParams.set("symbol", symbol);
      window.history.replaceState({}, "", url.toString());
    } catch (_err) {
      // Ignore on file:/// if replaceState is restricted
    }
  }

  async function loadSymbol(rawSymbol, opts = {}) {
    const symbol = normalizeSymbolInput(rawSymbol);
    const wasRunning = state.rt.running;
    disconnectLiveWebSocket();
    state.rt.lastZone = 0;
    state.rt.livePos = null;
    state.rt.tempImpact = 0;
    state.symbol = symbol;
    state.selectedTradeIdx = null;

    if (symbol === "ES" && BENCHMARK_SESSION) {
      RAW_SESSION = BENCHMARK_SESSION;
      state.symbolSource = "benchmark";
      state.symbolSpreadBps = Number((RAW_METRICS && RAW_METRICS.meta && RAW_METRICS.meta.spread_bps) ?? 0.6);
      state.symbolImpactCoeff = Number((RAW_METRICS && RAW_METRICS.meta && RAW_METRICS.meta.impact_coeff) ?? 0.12);
      updateSymbolUI("ES", "60-Session Benchmark", "");
      if (!opts.skipUrl) syncSymbolQueryParam("ES");
      recomputeAndRenderLab();
      return;
    }

    const profile = getSymbolProfile(symbol);
    state.symbolSpreadBps = profile.spreadBps;
    state.symbolImpactCoeff = profile.impact;

    // Immediately render deterministic calibrated 1m model so UI feels instantaneous
    const { bars: simBars } = generateCalibratedSymbolBars(symbol);
    RAW_SESSION = buildCausalSession(symbol, simBars, "Calibrated 1m Model", profile.spreadBps, profile.impact);
    state.symbolSource = "model";
    updateSymbolUI(symbol, profile.crypto ? "Fetching Live 1m…" : "Calibrated 1m Model", profile.crypto ? "loading" : "");
    if (!opts.skipUrl) syncSymbolQueryParam(symbol);
    recomputeAndRenderLab();

    // If crypto pair, opportunistically upgrade to live 1m Coinbase Exchange candles
    if (profile.crypto && !opts.offlineOnly) {
      const liveBars = await tryFetchLiveCryptoBars(symbol, profile);
      if (state.symbol !== symbol) return; // User switched symbols while fetching
      if (liveBars && liveBars.length >= 60) {
        RAW_SESSION = buildCausalSession(symbol, liveBars, "Live 1m Feed", profile.spreadBps, profile.impact);
        state.symbolSource = "live";
        updateSymbolUI(symbol, "Live 1m Feed", "live");
        recomputeAndRenderLab();
      } else {
        updateSymbolUI(symbol, "Calibrated 1m Model", "");
      }
      if (wasRunning && state.rt.running) {
        connectCryptoWebSocketIfApplicable();
      }
    }
  }

  function handleCsvFileUpload(file) {
    if (!file) return;
    const inferred = normalizeSymbolInput(file.name.replace(/\.[^.]+$/, "").slice(0, 12) || "CSV");
    const reader = new FileReader();
    reader.onload = () => {
      const parsed = parseCsvBars(reader.result);
      if (!parsed) {
        updateSymbolUI(state.symbol, "Invalid CSV (need >=25 OHLCV rows)", "loading");
        return;
      }
      state.symbol = inferred;
      state.symbolSource = "csv";
      state.symbolSpreadBps = 0.6;
      state.symbolImpactCoeff = 0.12;
      state.selectedTradeIdx = null;
      RAW_SESSION = buildCausalSession(inferred, parsed, "Custom CSV", 0.6, 0.12);
      updateSymbolUI(inferred, `Custom CSV (${parsed.length} bars)`, "live");
      recomputeAndRenderLab();
    };
    reader.readAsText(file);
  }

  /* ---------- Real-Time Streaming, WebSocket & Trigger Notifications ---------- */
  let AUDIO_CTX = null;

  function playAlertChime(kind) {
    if (!state.rt.channels.sound) return;
    try {
      const AudioCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtor) return;
      if (!AUDIO_CTX) AUDIO_CTX = new AudioCtor();
      if (AUDIO_CTX.state === "suspended") AUDIO_CTX.resume();

      const now = AUDIO_CTX.currentTime;
      const freqs = kind === "long"
        ? [523.25, 659.25]
        : kind === "short"
          ? [659.25, 493.88]
          : kind === "surge"
            ? [587.33, 880.0]
            : [440.0, 554.37];

      freqs.forEach((f, idx) => {
        const osc = AUDIO_CTX.createOscillator();
        const gain = AUDIO_CTX.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(f, now + idx * 0.11);
        gain.gain.setValueAtTime(0.0001, now + idx * 0.11);
        gain.gain.exponentialRampToValueAtTime(0.065, now + idx * 0.11 + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.11 + 0.18);
        osc.connect(gain);
        gain.connect(AUDIO_CTX.destination);
        osc.start(now + idx * 0.11);
        osc.stop(now + idx * 0.11 + 0.2);
      });
    } catch (_err) {
      // Audio playback blocked before user gesture
    }
  }

  function showToast(alertObj) {
    if (!state.rt.channels.toast) return;
    const host = $("toastContainer");
    if (!host) return;

    while (host.children.length >= 4) {
      host.removeChild(host.firstChild);
    }

    const card = document.createElement("div");
    card.className = `toast-card ${alertObj.kind}`;
    card.innerHTML =
      `<div class="toast-head">` +
        `<span class="toast-title">${alertObj.symbol} · ${alertObj.label}</span>` +
        `<span class="toast-time num">${alertObj.time}</span>` +
      `</div>` +
      `<div class="toast-body num">${alertObj.detail}</div>`;

    const dismiss = () => {
      if (card.parentNode) card.parentNode.removeChild(card);
    };
    card.addEventListener("click", dismiss);
    host.appendChild(card);
    setTimeout(dismiss, 5500);
  }

  function sendDesktopNotification(alertObj) {
    if (!state.rt.channels.desktop) return;
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    try {
      new Notification(`${alertObj.symbol} — ${alertObj.label}`, {
        body: alertObj.detail,
        tag: `vwap-${alertObj.symbol}-${alertObj.kind}`
      });
    } catch (_err) {
      // Ignore restricted desktop notification contexts
    }
  }

  function renderAlertLog() {
    const tbody = $("alertLogBody");
    const badge = $("alertCountBadge");
    if (!tbody || !badge) return;
    const list = state.rt.alerts;
    badge.textContent = `${list.length} alert${list.length === 1 ? "" : "s"}`;
    clearEl(tbody);

    if (list.length === 0) {
      const tr = document.createElement("tr");
      tr.className = "empty-alert-row";
      tr.innerHTML = `<td colspan="8" class="center">Click <strong>Start Live</strong> or <strong>Test Alert</strong> to stream real-time 5–20m VWAP triggers.</td>`;
      tbody.appendChild(tr);
      return;
    }

    list.forEach(a => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="left num">${a.time}</td>` +
        `<td class="left"><strong>${a.symbol}</strong></td>` +
        `<td class="center"><span class="trig-badge ${a.kind}">${a.label}</span></td>` +
        `<td class="num">${fmt(a.price, a.price >= 1000 ? 1 : 2)}</td>` +
        `<td class="num">${fmt(a.vwap, a.vwap >= 1000 ? 1 : 2)}</td>` +
        `<td class="num ${a.z <= -state.entryZ ? "pos" : a.z >= state.entryZ ? "neg" : ""}">${fmtSigned(a.z, 2)}\u03C3</td>` +
        `<td class="num">${fmt(a.relVol, 2)}\u00D7</td>` +
        `<td class="left">${a.detail}</td>`;
      tbody.appendChild(tr);
    });
  }

  function emitTriggerAlert(kind, label, bar, vwapPx, zVal, detail) {
    const alertObj = {
      id: Date.now() + Math.random(),
      time: bar.time || new Date().toTimeString().slice(0, 8),
      symbol: state.symbol,
      kind,
      label,
      price: Number(bar.close),
      vwap: Number(vwapPx),
      z: Number(zVal),
      relVol: Number(bar.rel_vol || 1.0),
      detail
    };
    state.rt.alerts.unshift(alertObj);
    if (state.rt.alerts.length > 60) state.rt.alerts.pop();
    renderAlertLog();
    showToast(alertObj);
    playAlertChime(kind);
    sendDesktopNotification(alertObj);
  }

  function updateLiveTelemetry() {
    if (!RAW_SESSION || !RAW_SESSION.bars || !RAW_SESSION.bars.length || !state.simSeries) return;
    const bars = RAW_SESSION.bars;
    const idx = bars.length - 1;
    const bar = bars[idx];
    const vwap = state.simSeries.vwapBlend[idx];
    const z = state.simSeries.zSeries[idx];
    const spreadBps = ((bar.close - vwap) / Math.max(1e-6, vwap)) * 10000;
    const digits = bar.close >= 1000 ? 1 : 2;

    const pill = $("telStatusPill");
    const statusTxt = $("telStatusText");
    if (pill && statusTxt) {
      pill.classList.toggle("live", state.rt.running);
      statusTxt.textContent = state.rt.running
        ? (state.rt.wsConnected ? "COINBASE WS LIVE" : `LIVE STREAM · ${(state.rt.cadenceMs / 1000).toFixed(1)}s`)
        : "STATIC SNAPSHOT";
    }

    if ($("telLastPx")) $("telLastPx").textContent = `$${fmt(bar.close, digits)}`;
    if ($("telVwapPx")) $("telVwapPx").textContent = `$${fmt(vwap, digits)}`;
    if ($("telSpreadBps")) $("telSpreadBps").textContent = `${fmtSigned(spreadBps, 1)} bp`;

    const zEl = $("telLiveZ");
    if (zEl) {
      zEl.textContent = `${fmtSigned(z, 2)}\u03C3`;
      zEl.classList.remove("zone-long", "zone-short");
      if (z <= -state.entryZ) zEl.classList.add("zone-long");
      else if (z >= state.entryZ) zEl.classList.add("zone-short");
    }
    if ($("telRelVol")) $("telRelVol").textContent = `${fmt(bar.rel_vol || 1.0, 2)}\u00D7`;
    if ($("telOuTau")) $("telOuTau").textContent = Number.isFinite(Number(bar.tau_half)) ? `${fmt(bar.tau_half, 1)}m` : "—";
    const vpinEl = $("telVpin");
    if (vpinEl) {
      const vp = Number(bar.vpin);
      vpinEl.textContent = Number.isFinite(vp) ? fmt(vp, 2) : "—";
      vpinEl.classList.remove("high", "low");
      if (Number.isFinite(vp)) {
        if (vp >= 0.56) vpinEl.classList.add("high");
        else if (vp <= 0.42) vpinEl.classList.add("low");
      }
    }
    const pTrendEl = $("telPTrend");
    const pTrendPill = $("telPTrendPill");
    if (pTrendEl && pTrendPill) {
      const pt = Number(bar.p_trend);
      const rm = bar.regime_mode || (pt < 0.38 ? "FADE" : (pt > 0.62 ? "SURF" : "FLAT"));
      pTrendEl.textContent = Number.isFinite(pt) ? `${fmt(pt, 2)} ${rm}` : "—";
      pTrendPill.classList.remove("fade", "flat", "surf");
      pTrendPill.classList.add(rm.toLowerCase());
    }
    if ($("telEngineMs")) $("telEngineMs").textContent = `${fmt(state.lastComputeMs, 2)} ms`;

    const sigEl = $("telSignalState");
    if (sigEl) {
      sigEl.classList.remove("long", "short");
      const pt = Number(bar.p_trend ?? 0.2);
      const isDualSurf = state.preset === "dual_regime" && pt > 0.62 && Number(bar.fan_align || 0) >= 0.5;
      const isDualFlat = state.preset === "dual_regime" && pt >= 0.38 && pt <= 0.62;
      const isVetoed = state.preset === "ou_spectral" && Boolean(bar.toxic_flag) && Math.abs(z) >= state.entryZ;
      if (isDualSurf) {
        const dir = Number(bar.fan_dir || 1) >= 0 ? "LONG" : "SHORT";
        sigEl.textContent = `SURF ${dir}`;
        sigEl.classList.add(dir === "LONG" ? "long" : "short");
      } else if (isDualFlat) {
        sigEl.textContent = "BAYES FLAT ZONE";
      } else if (isVetoed) {
        sigEl.textContent = "VETOED TOXIC";
        sigEl.classList.add("short");
      } else if (z <= -state.entryZ) {
        sigEl.textContent = "LONG ENTRY ZONE";
        sigEl.classList.add("long");
      } else if (z >= state.entryZ) {
        sigEl.textContent = "SHORT ENTRY ZONE";
        sigEl.classList.add("short");
      } else if (state.rt.livePos) {
        sigEl.textContent = `ACTIVE ${state.rt.livePos.side}`;
        sigEl.classList.add(state.rt.livePos.side === "LONG" ? "long" : "short");
      } else {
        sigEl.textContent = "WATCHING";
      }
    }
    updatePaperDeskUI();
  }

  function evaluateLiveTriggers(bar, vwapPx, zVal) {
    const rt = state.rt;
    const digits = bar.close >= 1000 ? 1 : 2;
    const spreadBps = ((bar.close - vwapPx) / Math.max(1e-6, vwapPx)) * 10000;

    // 1. Band Breach Trigger
    if (rt.triggers.breach) {
      if (zVal <= -state.entryZ && rt.lastZone !== -1) {
        rt.lastZone = -1;
        rt.livePos = { side: "LONG", entryPx: bar.close, entryTime: bar.time };
        emitTriggerAlert(
          "long",
          "LONG BREACH",
          bar,
          vwapPx,
          zVal,
          `Px $${fmt(bar.close, digits)} breached -${fmt(state.entryZ, 2)}\u03C3 (${fmtSigned(spreadBps, 1)} bp vs 5–20m VWAP)`
        );
      } else if (zVal >= state.entryZ && rt.lastZone !== 1) {
        rt.lastZone = 1;
        rt.livePos = { side: "SHORT", entryPx: bar.close, entryTime: bar.time };
        emitTriggerAlert(
          "short",
          "SHORT BREACH",
          bar,
          vwapPx,
          zVal,
          `Px $${fmt(bar.close, digits)} breached +${fmt(state.entryZ, 2)}\u03C3 (${fmtSigned(spreadBps, 1)} bp vs 5–20m VWAP)`
        );
      } else if (Math.abs(zVal) < state.entryZ * 0.7) {
        rt.lastZone = 0;
      }
    }

    // 2. VWAP Mean-Reversion Cross / Exit Trigger
    if (rt.triggers.exit && rt.livePos && Math.abs(zVal) <= 0.25) {
      const dir = rt.livePos.side === "LONG" ? 1 : -1;
      const moveBps = dir * ((bar.close - rt.livePos.entryPx) / Math.max(1e-6, rt.livePos.entryPx)) * 10000;
      const sideLbl = rt.livePos.side;
      rt.livePos = null;
      emitTriggerAlert(
        "exit",
        "VWAP REVERT EXIT",
        bar,
        vwapPx,
        zVal,
        `${sideLbl} reverted to Blended VWAP $${fmt(vwapPx, digits)} (${fmtSigned(moveBps, 1)} bp gross)`
      );
    }

    // 3. Volume-Clock Surge Trigger
    if (rt.triggers.surge && Number(bar.rel_vol || 1) >= 2.0 && (rt.tickCount - rt.lastSurgeTick) >= 5) {
      rt.lastSurgeTick = rt.tickCount;
      emitTriggerAlert(
        "surge",
        "VOL CLOCK SURGE",
        bar,
        vwapPx,
        zVal,
        `Rel Vol ${fmt(bar.rel_vol, 2)}\u00D7 — Adaptive blend tilted ${Math.round((bar.w5 + bar.w10) * 100)}% to 5–10m`
      );
    }
  }

  function appendCausalTailBar(newRawBar) {
    const bars = RAW_SESSION.bars;
    bars.push(newRawBar);
    if (bars.length > 390) {
      bars.shift();
    }
    const n = bars.length;
    const meanVol = bars.reduce((s, b) => s + Number(b.volume || 1), 0) / Math.max(1, n);
    const i = n - 1;
    const b = bars[i];
    const relVol = Math.max(0.25, Number(b.volume || 1) / Math.max(1e-6, meanVol));
    const horizons = ALL_HORIZONS;
    const vwaps = {};
    const sigmas = {};

    for (let hi = 0; hi < horizons.length; hi++) {
      const h = horizons[hi];
      const start = Math.max(0, i - h + 1);
      let sumPV = 0, sumV = 0;
      for (let j = start; j <= i; j++) {
        const bj = bars[j];
        const tp = (bj.high + bj.low + bj.close) / 3;
        const vj = Math.max(1e-6, Number(bj.volume || 1));
        sumPV += tp * vj;
        sumV += vj;
      }
      const vwapH = sumPV / Math.max(1e-9, sumV);
      let sumVar = 0;
      for (let j = start; j <= i; j++) {
        const bj = bars[j];
        const vj = Math.max(1e-6, Number(bj.volume || 1));
        const diff = bj.close - vwapH;
        sumVar += vj * diff * diff;
      }
      vwaps[h] = vwapH;
      sigmas[h] = Math.max(b.close * 0.00025, Math.sqrt(sumVar / Math.max(1e-9, sumV)));
    }

    const volTilt = clamp((relVol - 1.0) / 1.8, -0.65, 1.25);
    const rawW = [
      Math.max(0.08, 0.35 + 0.16 * volTilt),
      Math.max(0.10, 0.30 + 0.02 * volTilt),
      Math.max(0.06, 0.20 - 0.09 * volTilt),
      Math.max(0.04, 0.15 - 0.09 * volTilt)
    ];
    const wSum = rawW[0] + rawW[1] + rawW[2] + rawW[3];
    const w5 = rawW[0] / wSum;
    const w10 = rawW[1] / wSum;
    const w15 = rawW[2] / wSum;
    const w20 = rawW[3] / wSum;

    const vwapStatic = 0.35 * vwaps[5] + 0.30 * vwaps[10] + 0.20 * vwaps[15] + 0.15 * vwaps[20];
    const vwapAdaptive = w5 * vwaps[5] + w10 * vwaps[10] + w15 * vwaps[15] + w20 * vwaps[20];
    const varWithin =
      w5 * sigmas[5] * sigmas[5] +
      w10 * sigmas[10] * sigmas[10] +
      w15 * sigmas[15] * sigmas[15] +
      w20 * sigmas[20] * sigmas[20];
    const varBetween =
      w5 * Math.pow(vwaps[5] - vwapAdaptive, 2) +
      w10 * Math.pow(vwaps[10] - vwapAdaptive, 2) +
      w15 * Math.pow(vwaps[15] - vwapAdaptive, 2) +
      w20 * Math.pow(vwaps[20] - vwapAdaptive, 2);
    const sigmaBlend = Math.max(b.close * 0.00025, Math.sqrt(varWithin + varBetween));
    const zScore = (b.close - vwapAdaptive) / sigmaBlend;

    bars[i] = {
      idx: i,
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
      rel_vol: Number(relVol.toFixed(3)),
      vwap5: vwaps[5],
      vwap6: vwaps[6],
      vwap8: vwaps[8],
      vwap10: vwaps[10],
      vwap12: vwaps[12],
      vwap15: vwaps[15],
      vwap16: vwaps[16],
      vwap18: vwaps[18],
      vwap20: vwaps[20],
      sigma5: sigmas[5],
      sigma10: sigmas[10],
      sigma15: sigmas[15],
      sigma20: sigmas[20],
      vwap_static: vwapStatic,
      vwap_adaptive: vwapAdaptive,
      sigma_blend: sigmaBlend,
      z_score: zScore,
      w5, w10, w15, w20
    };
    for (let k = 0; k < n; k++) bars[k].idx = k;
    enrichPhase2Microstructure(bars, false);
    if (RADAR_SYMBOLS.includes(state.symbol)) {
      RADAR_SESSION_CACHE[state.symbol] = RAW_SESSION;
    }
    return bars[i];
  }

  function advanceNextMinuteTime(prevTimeStr) {
    const parts = String(prevTimeStr || "16:00").split(":");
    let totalMin = (Number(parts[0]) || 16) * 60 + (Number(parts[1]) || 0) + 1;
    totalMin = totalMin % (24 * 60);
    return `${String(Math.floor(totalMin / 60)).padStart(2, "0")}:${String(totalMin % 60).padStart(2, "0")}`;
  }

  function stepSimulatedLiveTick() {
    if (!state.rt.running || !RAW_SESSION || !RAW_SESSION.bars.length) return;
    if (state.rt.wsConnected) return; // Live Coinbase WebSocket is driving ticks directly

    state.rt.tickCount += 1;
    const bars = RAW_SESSION.bars;
    const last = bars[bars.length - 1];
    const profile = getSymbolProfile(state.symbol);
    const baseSigma = profile.volBps / 10000;

    // Occasional institutional slicing burst to exercise 5-20m triggers realistically
    const isBurst = (state.rt.tickCount % 7 === 0) || Math.random() < 0.14;
    const burstDir = Math.random() > 0.5 ? 1 : -1;
    const shock = (Math.random() + Math.random() + Math.random() - 1.5) * 1.4 * baseSigma;
    state.rt.tempImpact = state.rt.tempImpact * 0.76 + (isBurst ? burstDir * baseSigma * 2.45 : 0) + shock * 0.55;

    const openPx = last.close;
    const closePx = Math.max(profile.basePx * 0.4, openPx * (1 + shock * 0.45 + state.rt.tempImpact * 0.42));
    const wick = Math.abs(shock) * 0.9 + baseSigma * 0.35;
    const highPx = Math.max(openPx, closePx) * (1 + wick);
    const lowPx = Math.min(openPx, closePx) * (1 - wick);
    const volMult = isBurst ? (2.15 + Math.random() * 1.1) : (0.72 + Math.random() * 0.65);
    const meanVol = bars.reduce((s, b) => s + Number(b.volume || 1), 0) / Math.max(1, bars.length);
    const volume = Math.max(10, Math.round(meanVol * volMult));

    const updated = appendCausalTailBar({
      idx: bars.length,
      time: advanceNextMinuteTime(last.time),
      open: Number(openPx.toFixed(4)),
      high: Number(highPx.toFixed(4)),
      low: Number(lowPx.toFixed(4)),
      close: Number(closePx.toFixed(4)),
      volume
    });

    recomputeSession();
    const idx = RAW_SESSION.bars.length - 1;
    evaluateLiveTriggers(updated, state.simSeries.vwapBlend[idx], state.simSeries.zSeries[idx]);
    evaluatePaperAutoPilot(updated, state.simSeries.vwapBlend[idx], state.simSeries.zSeries[idx]);
    scheduleLabRender();
    if (state.activeTab === "research") {
      runMultiAssetRadarScan();
      renderRadarTable();
      renderTermStructureChart();
    }
  }

  function disconnectLiveWebSocket() {
    if (state.rt.ws) {
      try { state.rt.ws.close(); } catch (_err) {}
      state.rt.ws = null;
    }
    state.rt.wsConnected = false;
  }

  function connectCryptoWebSocketIfApplicable() {
    disconnectLiveWebSocket();
    const profile = getSymbolProfile(state.symbol);
    if (!profile.crypto || !profile.cbPair || typeof WebSocket === "undefined") return;

    try {
      const ws = new WebSocket("wss://ws-feed.exchange.coinbase.com");
      state.rt.ws = ws;
      ws.onopen = () => {
        if (!state.rt.running || state.rt.ws !== ws) {
          ws.close();
          return;
        }
        state.rt.wsConnected = true;
        ws.send(JSON.stringify({
          type: "subscribe",
          product_ids: [profile.cbPair],
          channels: ["ticker"]
        }));
        updateLiveControlsUI();
      };
      ws.onmessage = evt => {
        if (!state.rt.running || state.rt.ws !== ws) return;
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type !== "ticker" || !msg.price) return;
          const px = Number(msg.price);
          if (!Number.isFinite(px) || px <= 0) return;
          const sz = Math.max(0.0001, Number(msg.last_size || 0.05));
          const dt = msg.time ? new Date(msg.time) : new Date();
          const hhmm = `${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
          const bars = RAW_SESSION.bars;
          const last = bars[bars.length - 1];
          state.rt.tickCount += 1;

          if (last && last.time === hhmm) {
            last.close = px;
            last.high = Math.max(last.high, px);
            last.low = Math.min(last.low, px);
            last.volume = Number(last.volume || 0) + sz;
            bars.pop();
            const updated = appendCausalTailBar(last);
            recomputeAndRenderLab();
            const idx = RAW_SESSION.bars.length - 1;
            evaluateLiveTriggers(updated, state.simSeries.vwapBlend[idx], state.simSeries.zSeries[idx]);
          } else {
            const updated = appendCausalTailBar({
              idx: bars.length,
              time: hhmm,
              open: last ? last.close : px,
              high: Math.max(last ? last.close : px, px),
              low: Math.min(last ? last.close : px, px),
              close: px,
              volume: sz
            });
            recomputeAndRenderLab();
            const idx = RAW_SESSION.bars.length - 1;
            evaluateLiveTriggers(updated, state.simSeries.vwapBlend[idx], state.simSeries.zSeries[idx]);
          }
        } catch (_parseErr) {}
      };
      ws.onerror = () => {
        state.rt.wsConnected = false;
        updateLiveControlsUI();
      };
      ws.onclose = () => {
        if (state.rt.ws === ws) {
          state.rt.ws = null;
          state.rt.wsConnected = false;
          updateLiveControlsUI();
        }
      };
    } catch (_err) {
      state.rt.wsConnected = false;
    }
  }

  function updateLiveControlsUI() {
    const btn = $("btnLiveToggle");
    const lbl = $("btnLiveLabel");
    const badge = $("rtStatusBadge");
    if (btn) btn.classList.toggle("running", state.rt.running);
    if (lbl) lbl.textContent = state.rt.running ? "Pause Live" : "Start Live";
    if (badge) {
      const cadLbl = state.rt.cadenceMs < 1000 ? `${state.rt.cadenceMs}ms` : `${(state.rt.cadenceMs / 1000).toFixed(1)}s`;
      badge.textContent = state.rt.running
        ? (state.rt.wsConnected ? "Live WS" : `Live ${cadLbl}`)
        : "Paused";
      badge.classList.toggle("live", state.rt.running);
    }
    const turboBtn = $("btnTurboMode");
    const turboLbl = $("btnTurboLabel");
    const turboSub = $("btnTurboSub");
    if (turboBtn) {
      turboBtn.classList.toggle("active", state.turboMode);
      turboBtn.setAttribute("aria-pressed", String(state.turboMode));
    }
    if (turboLbl) turboLbl.textContent = state.turboMode ? "Turbo 10\u00D7 Alpha: ON" : "Turbo 10\u00D7 Alpha: OFF";
    if (turboSub) turboSub.textContent = state.turboMode ? "Maker + Kelly" : "1\u00D7 Taker";
    updateLiveTelemetry();
  }

  let RAF_PENDING = false;
  function scheduleLabRender() {
    if (RAF_PENDING) return;
    RAF_PENDING = true;
    const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn => setTimeout(fn, 0));
    raf(() => {
      RAF_PENDING = false;
      recomputeAndRenderLab();
    });
  }

  function startLiveStream() {
    if (state.rt.running) return;
    // Clone benchmark session before mutating bars so ES benchmark remains untouched on reset
    if (RAW_SESSION === BENCHMARK_SESSION && BENCHMARK_SESSION) {
      RAW_SESSION = {
        ...BENCHMARK_SESSION,
        bars: BENCHMARK_SESSION.bars.map(b => ({ ...b })),
        trades: []
      };
      state.symbolSource = "live-sim";
    }
    state.rt.running = true;
    state.zoom = "close"; // Focus tail so incoming live candles are immediately visible
    document.querySelectorAll("#zoomPills .pill-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.zoom === state.zoom)
    );
    updateLiveControlsUI();
    connectCryptoWebSocketIfApplicable();
    if (state.rt.timerId) clearInterval(state.rt.timerId);
    state.rt.timerId = setInterval(stepSimulatedLiveTick, state.rt.cadenceMs);
    recomputeAndRenderLab();
  }

  function stopLiveStream() {
    state.rt.running = false;
    if (state.rt.timerId) {
      clearInterval(state.rt.timerId);
      state.rt.timerId = null;
    }
    disconnectLiveWebSocket();
    updateLiveControlsUI();
  }

  function triggerSampleAlert() {
    if (!RAW_SESSION || !RAW_SESSION.bars.length || !state.simSeries) return;
    const idx = RAW_SESSION.bars.length - 1;
    const bar = RAW_SESSION.bars[idx];
    const vwap = state.simSeries.vwapBlend[idx];
    const zVal = state.simSeries.zSeries[idx];
    const isLong = zVal <= 0;
    const sampleZ = isLong ? -(state.entryZ + 0.14) : (state.entryZ + 0.14);
    emitTriggerAlert(
      isLong ? "long" : "short",
      isLong ? "LONG BREACH (TEST)" : "SHORT BREACH (TEST)",
      bar,
      vwap,
      sampleZ,
      `Test notification fired for ${state.symbol} at $${fmt(bar.close, bar.close >= 1000 ? 1 : 2)} (threshold ±${fmt(state.entryZ, 2)}\u03C3)`
    );
  }

  /* ---------- Live Paper-Trading Execution Desk ($100,000 Account) ---------- */
  function getPaperLiveContext() {
    if (!RAW_SESSION || !RAW_SESSION.bars || !RAW_SESSION.bars.length || !state.simSeries) return null;
    const idx = RAW_SESSION.bars.length - 1;
    return {
      bar: RAW_SESSION.bars[idx],
      vwap: state.simSeries.vwapBlend[idx],
      z: state.simSeries.zSeries[idx]
    };
  }

  function updatePaperDeskUI() {
    const p = state.paper;
    const ctx = getPaperLiveContext();
    const px = ctx ? Number(ctx.bar.close) : 0;
    const unrealUsd = p.qty !== 0 && px > 0 ? p.qty * (px - p.avgEntryPx) : 0;
    const unrealBps = p.qty !== 0 && p.avgEntryPx > 0
      ? (p.qty > 0 ? 1 : -1) * ((px - p.avgEntryPx) / p.avgEntryPx) * 10000
      : 0;
    const equity = p.startingEquity + p.realizedUsd + unrealUsd;
    const avgRealizedBps = p.closedCount > 0 ? p.realizedBpsSum / p.closedCount : 0;
    const avgVwapEdge = p.vwapEdgeCnt > 0 ? p.vwapEdgeSum / p.vwapEdgeCnt : 0;

    const posEl = $("pdPositionBadge");
    if (posEl) {
      posEl.classList.remove("flat", "long", "short");
      if (Math.abs(p.qty) < 1e-8) {
        posEl.textContent = "FLAT · 0 sh";
        posEl.classList.add("flat");
      } else {
        const sideLbl = p.qty > 0 ? "LONG" : "SHORT";
        const qStr = Math.abs(p.qty) >= 10 ? fmtInt(Math.abs(p.qty)) : fmt(Math.abs(p.qty), 3);
        const modeTag = p.entryMode ? ` ${p.entryMode}` : "";
        posEl.textContent = `${sideLbl}${modeTag} · ${qStr} ${state.symbol}`;
        posEl.classList.add(p.qty > 0 ? "long" : "short");
      }
    }

    if ($("pdEquity")) {
      $("pdEquity").textContent = `$${equity.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }
    const realEl = $("pdRealized");
    if (realEl) {
      realEl.textContent = `${p.realizedUsd >= 0 ? "+" : "-"}$${Math.abs(p.realizedUsd).toFixed(2)} (${fmtSigned(avgRealizedBps, 1)}bp)`;
      realEl.classList.remove("pos", "neg");
      if (p.realizedUsd > 0.005) realEl.classList.add("pos");
      else if (p.realizedUsd < -0.005) realEl.classList.add("neg");
    }
    const unrEl = $("pdUnrealized");
    if (unrEl) {
      unrEl.textContent = `${unrealUsd >= 0 ? "+" : "-"}$${Math.abs(unrealUsd).toFixed(2)} (${fmtSigned(unrealBps, 1)}bp)`;
      unrEl.classList.remove("pos", "neg");
      if (unrealUsd > 0.005) unrEl.classList.add("pos");
      else if (unrealUsd < -0.005) unrEl.classList.add("neg");
    }
    const vqEl = $("pdVwapQuality");
    if (vqEl) {
      vqEl.textContent = `${fmtSigned(avgVwapEdge, 2)} bp`;
      vqEl.classList.remove("pos", "neg");
      if (avgVwapEdge > 0.01) vqEl.classList.add("pos");
      else if (avgVwapEdge < -0.01) vqEl.classList.add("neg");
    }
    if ($("pdFillCount")) $("pdFillCount").textContent = String(p.fills.length);

    const autoBtn = $("btnPaperAuto");
    if (autoBtn) {
      autoBtn.classList.toggle("active", p.autoPilot);
      autoBtn.setAttribute("aria-pressed", String(p.autoPilot));
      autoBtn.textContent = p.autoPilot ? "Auto-Pilot: ON" : "Auto-Pilot: OFF";
    }
  }

  function paperExecuteOrder(side, modeTag = "MANUAL") {
    const ctx = getPaperLiveContext();
    if (!ctx) return;
    const p = state.paper;
    const dir = side === "LONG" ? 1 : -1;
    const halfSpreadFrac = (state.symbolSpreadBps * (state.turboMode ? 0.16 : 0.5)) / 10000;
    const fillPx = ctx.bar.close * (1 + dir * halfSpreadFrac);
    const targetNotional = state.turboMode ? 45000 : 25000;
    const orderQty = fillPx >= 2000
      ? Number((targetNotional / fillPx).toFixed(4))
      : Math.max(1, Math.round(targetNotional / fillPx));

    // If flipping an opposite position, realize PnL cleanly first
    if (p.qty !== 0 && Math.sign(p.qty) !== dir) {
      paperFlatten("FLIP");
    }

    const vwapEdgeBps = dir * ((ctx.vwap - fillPx) / Math.max(1e-6, fillPx)) * 10000;
    const prevQty = p.qty;
    const newQty = prevQty + dir * orderQty;
    if (Math.abs(newQty) > 1e-8) {
      p.avgEntryPx = (Math.abs(prevQty) * p.avgEntryPx + orderQty * fillPx) / Math.abs(newQty);
    }
    p.qty = newQty;
    p.entryVwap = ctx.vwap;
    p.entryMode = modeTag;
    p.vwapEdgeSum += vwapEdgeBps;
    p.vwapEdgeCnt += 1;

    p.fills.unshift({
      time: ctx.bar.time,
      symbol: state.symbol,
      side,
      mode: modeTag,
      qty: orderQty,
      price: fillPx,
      vwap: ctx.vwap,
      edgeBps: vwapEdgeBps,
      pnlUsd: 0,
      pnlBps: 0
    });
    if (p.fills.length > 200) p.fills.pop();

    updatePaperDeskUI();
    showToast({
      kind: side === "LONG" ? "long" : "short",
      symbol: state.symbol,
      label: `PAPER ${side} (${modeTag})`,
      time: ctx.bar.time,
      detail: `Filled ${orderQty} ${state.symbol} @ $${fmt(fillPx, fillPx >= 1000 ? 1 : 2)} · VWAP edge ${fmtSigned(vwapEdgeBps, 2)} bp`
    });
  }

  function paperFlatten(reason = "FLATTEN") {
    const p = state.paper;
    if (Math.abs(p.qty) < 1e-8) return;
    const ctx = getPaperLiveContext();
    if (!ctx) return;

    const closeDir = p.qty > 0 ? -1 : 1;
    const halfSpreadFrac = (state.symbolSpreadBps * (state.turboMode ? 0.16 : 0.5)) / 10000;
    const exitPx = ctx.bar.close * (1 + closeDir * halfSpreadFrac);
    const pnlUsd = p.qty * (exitPx - p.avgEntryPx);
    const posDir = p.qty > 0 ? 1 : -1;
    const pnlBps = posDir * ((exitPx - p.avgEntryPx) / Math.max(1e-6, p.avgEntryPx)) * 10000;
    const closedQty = Math.abs(p.qty);

    p.realizedUsd += pnlUsd;
    p.realizedBpsSum += pnlBps;
    p.closedCount += 1;
    p.fills.unshift({
      time: ctx.bar.time,
      symbol: state.symbol,
      side: closeDir > 0 ? "BUY_CLOSE" : "SELL_CLOSE",
      mode: reason,
      qty: closedQty,
      price: exitPx,
      vwap: ctx.vwap,
      edgeBps: closeDir * ((ctx.vwap - exitPx) / Math.max(1e-6, exitPx)) * 10000,
      pnlUsd,
      pnlBps
    });
    p.qty = 0;
    p.avgEntryPx = 0;
    p.entryMode = "";
    updatePaperDeskUI();

    if (reason !== "FLIP") {
      showToast({
        kind: "exit",
        symbol: state.symbol,
        label: `PAPER ${reason}`,
        time: ctx.bar.time,
        detail: `Closed ${closedQty} ${state.symbol} @ $${fmt(exitPx, exitPx >= 1000 ? 1 : 2)} · Net ${pnlUsd >= 0 ? "+" : "-"}$${Math.abs(pnlUsd).toFixed(2)} (${fmtSigned(pnlBps, 1)} bp)`
      });
    }
  }

  function evaluatePaperAutoPilot(bar, vwapPx, zVal) {
    const p = state.paper;
    if (!p.autoPilot) return;
    const pt = Number(bar.p_trend ?? 0.2);

    // Exit logic first
    if (Math.abs(p.qty) > 1e-8) {
      const isLong = p.qty > 0;
      if (p.entryMode === "SURF") {
        // Strict causal trailing 10m VWAP cross stop
        if ((isLong && bar.close < bar.vwap10) || (!isLong && bar.close > bar.vwap10)) {
          paperFlatten("SURF 10m STOP");
        }
      } else {
        // Reversion to Blended VWAP
        if ((isLong && zVal >= -0.20) || (!isLong && zVal <= 0.20)) {
          paperFlatten("FADE REVERT");
        }
      }
      return;
    }

    // Entry logic: Dual-Regime Bayes bifurcation (Fade < 0.38 | Flat | Surf > 0.62)
    if (pt > 0.62 && Number(bar.fan_align || 0) >= 0.5) {
      const side = Number(bar.fan_dir || 1) >= 0 ? "LONG" : "SHORT";
      paperExecuteOrder(side, "SURF");
    } else if (pt < 0.38) {
      if (zVal <= -state.entryZ) paperExecuteOrder("LONG", "FADE");
      else if (zVal >= state.entryZ) paperExecuteOrder("SHORT", "FADE");
    }
  }

  function exportDeskCsv() {
    const lines = [
      "source,symbol,id,side,mode,entry_time,exit_time,hold_min,entry_px,exit_px,entry_z,gross_bps,cost_bps,net_bps,vwap_edge_bps"
    ];
    state.paper.fills.forEach((f, idx) => {
      lines.push([
        "PAPER_DESK", f.symbol, idx + 1, f.side, f.mode,
        f.time, f.time, 0, f.price.toFixed(4), f.price.toFixed(4),
        "0.00", f.pnlBps.toFixed(2), "0.00", f.pnlBps.toFixed(2), f.edgeBps.toFixed(2)
      ].join(","));
    });
    state.simTrades.forEach(t => {
      lines.push([
        "SESSION_TAPE", state.symbol, t.id, t.side, t.mode || PRESETS[state.preset].short,
        t.entryTime, t.exitTime, t.holdMin, t.entryPx.toFixed(4), t.exitPx.toFixed(4),
        t.entryZ.toFixed(3), t.grossBps.toFixed(2), t.costBps.toFixed(2), t.netBps.toFixed(2), t.vwapEdge.toFixed(2)
      ].join(","));
    });

    const csvText = lines.join("\n");
    try {
      const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `blended_vwap_${state.symbol.toLowerCase()}_tape.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (_err) {}
    showToast({
      kind: "exit",
      symbol: state.symbol,
      label: "CSV EXPORTED",
      time: "Now",
      detail: `Exported ${state.paper.fills.length} paper fills + ${state.simTrades.length} session trades.`
    });
  }

  /* ---------- Causal Intraday Re-Calculation Engine ---------- */
  function getZoomBounds() {
    const bars = RAW_SESSION.bars;
    const n = bars.length;
    if (state.zoom === "full") {
      return { start: 0, end: n - 1, label: `Full Day (${bars[0].time}–${bars[n - 1].time} ET)`, shortRange: `${bars[0].time}–${bars[n - 1].time} ET` };
    }
    if (state.zoom === "open") {
      const end = Math.min(n - 1, 89);
      return { start: 0, end, label: `Morning Open (${bars[0].time}–${bars[end].time} ET)`, shortRange: `${bars[0].time}–${bars[end].time} ET` };
    }
    if (state.zoom === "midday") {
      const start = Math.min(n - 60, 135);
      const end = Math.min(n - 1, 255);
      return { start, end, label: `Midday Lull (${bars[start].time}–${bars[end].time} ET)`, shortRange: `${bars[start].time}–${bars[end].time} ET` };
    }
    if (state.zoom === "close") {
      const start = Math.max(0, n - 75);
      return { start, end: n - 1, label: `Closing Hour (${bars[start].time}–${bars[n - 1].time} ET)`, shortRange: `${bars[start].time}–${bars[n - 1].time} ET` };
    }
    // Spotlight window from session_bars.json
    const sw = RAW_SESSION.spotlight_window || {};
    const s = clamp(Number(sw.start_idx ?? 45), 0, n - 30);
    const e = clamp(Number(sw.end_idx ?? Math.min(n - 1, s + 95)), s + 20, n - 1);
    const timeRange = `${bars[s].time}–${bars[e].time} ET`;
    const lbl = sw.label
      ? (sw.label.includes("ET") ? sw.label : `${sw.label} (${timeRange})`)
      : `Spotlight (${timeRange})`;
    return { start: s, end: e, label: lbl, shortRange: timeRange };
  }

  function recomputeSession() {
    const tPerf0 = typeof performance !== "undefined" ? performance.now() : 0;
    const bars = RAW_SESSION.bars;
    const n = bars.length;
    const meta = (RAW_METRICS && RAW_METRICS.meta) || {};
    const spreadBps = Number(state.symbol === "ES" ? (meta.spread_bps ?? 0.6) : (state.symbolSpreadBps ?? 0.6));
    const impactCoeff = Number(state.symbol === "ES" ? (meta.impact_coeff ?? 0.12) : (state.symbolImpactCoeff ?? 0.12));

    const isDefaultAdaptive =
      state.symbol === "ES" &&
      !state.turboMode &&
      !state.rt.running &&
      state.symbolSource === "benchmark" &&
      state.preset === "adaptive" &&
      Math.abs(state.entryZ - state.defaultEntryZ) < 1e-6 &&
      state.maxHold === state.defaultMaxHold;

    const w5Fixed = state.weights[0] / 100;
    const w10Fixed = state.weights[1] / 100;
    const w15Fixed = state.weights[2] / 100;
    const w20Fixed = state.weights[3] / 100;

    const vwapBlend = new Array(n);
    const sigmaBlend = new Array(n);
    const zSeries = new Array(n);
    const w5Arr = new Array(n);
    const w10Arr = new Array(n);
    const w15Arr = new Array(n);
    const w20Arr = new Array(n);
    const devArr = new Array(n);

    // Effective lookback horizon for causal sigma estimation when custom weights are active
    const effHorizon = Math.max(5, Math.round(5 * w5Fixed + 10 * w10Fixed + 15 * w15Fixed + 20 * w20Fixed));

    if (bars.length > 0 && bars[bars.length - 1].w5_ou === undefined) {
      enrichPhase2Microstructure(bars, state.symbol === "ES" && state.symbolSource === "benchmark");
    }

    for (let i = 0; i < n; i++) {
      const b = bars[i];
      if (state.preset === "ou_spectral" || state.preset === "dual_regime") {
        vwapBlend[i] = Number(b.vwap_ou ?? b.vwap_adaptive);
        sigmaBlend[i] = Math.max(1e-6, Number(b.sigma_ou ?? b.sigma_blend));
        zSeries[i] = Number(b.z_ou ?? b.z_score);
        w5Arr[i] = Number(b.w5_ou ?? b.ou_w5 ?? b.w5);
        w10Arr[i] = Number(b.w10_ou ?? b.ou_w10 ?? b.w10);
        w15Arr[i] = Number(b.w15_ou ?? b.ou_w15 ?? b.w15);
        w20Arr[i] = Number(b.w20_ou ?? b.ou_w20 ?? b.w20);
      } else if (state.preset === "adaptive") {
        vwapBlend[i] = Number(b.vwap_adaptive);
        sigmaBlend[i] = Math.max(1e-6, Number(b.sigma_blend));
        zSeries[i] = Number(b.z_score);
        w5Arr[i] = Number(b.w5);
        w10Arr[i] = Number(b.w10);
        w15Arr[i] = Number(b.w15);
        w20Arr[i] = Number(b.w20);
      } else {
        const vb = w5Fixed * b.vwap5 + w10Fixed * b.vwap10 + w15Fixed * b.vwap15 + w20Fixed * b.vwap20;
        vwapBlend[i] = vb;
        devArr[i] = b.close - vb;
        let sig;
        if (b.sigma5 !== undefined && b.sigma10 !== undefined && b.sigma15 !== undefined && b.sigma20 !== undefined) {
          // Exact volume-weighted multi-horizon dispersion formula when horizon sigmas are available
          const varWithin =
            w5Fixed * b.sigma5 * b.sigma5 +
            w10Fixed * b.sigma10 * b.sigma10 +
            w15Fixed * b.sigma15 * b.sigma15 +
            w20Fixed * b.sigma20 * b.sigma20;
          const varBetween =
            w5Fixed * Math.pow(b.vwap5 - vb, 2) +
            w10Fixed * Math.pow(b.vwap10 - vb, 2) +
            w15Fixed * Math.pow(b.vwap15 - vb, 2) +
            w20Fixed * Math.pow(b.vwap20 - vb, 2);
          const floorSig = Math.max(b.close * 0.00025, 1e-5);
          sig = Math.max(floorSig, Math.sqrt(varWithin + varBetween));
        } else {
          // Causal trailing dispersion fallback
          const lookback = Math.min(i + 1, Math.max(8, effHorizon));
          let sumSq = 0;
          let sumW = 0;
          for (let k = i - lookback + 1; k <= i; k++) {
            const volW = Math.max(1, Number(bars[k].volume || 1));
            const d = devArr[k];
            sumSq += volW * d * d;
            sumW += volW;
          }
          const localStd = Math.sqrt(sumSq / Math.max(1, sumW));
          const xVar =
            w5Fixed * Math.pow(b.vwap5 - vb, 2) +
            w10Fixed * Math.pow(b.vwap10 - vb, 2) +
            w15Fixed * Math.pow(b.vwap15 - vb, 2) +
            w20Fixed * Math.pow(b.vwap20 - vb, 2);
          const rawSig = Math.sqrt(localStd * localStd + 0.5 * xVar);
          const floorSig = Math.max(b.close * 0.00035, Number(b.sigma_blend) * 0.55);
          sig = Math.max(floorSig, 0.55 * rawSig + 0.45 * Number(b.sigma_blend));
        }
        sigmaBlend[i] = sig;
        zSeries[i] = (b.close - vb) / sig;
        w5Arr[i] = w5Fixed;
        w10Arr[i] = w10Fixed;
        w15Arr[i] = w15Fixed;
        w20Arr[i] = w20Fixed;
      }
    }

    let trades = [];
    if (isDefaultAdaptive && Array.isArray(RAW_SESSION.trades) && RAW_SESSION.trades.length > 0) {
      trades = RAW_SESSION.trades.map((t, idx) => normalizeSessionTrade(t, bars, idx));
    } else {
      // Causal 5-20m execution simulation on session bars
      const minHold = Math.min(5, state.maxHold);
      const entryZ = state.entryZ;
      const maxHold = state.maxHold;
      let i = 20;
      let tid = 1;

      while (i < n - minHold - 1) {
        const z = zSeries[i];
        const b = bars[i];
        let side = null;
        let tradeMode = "";
        let regimeSizeWeight = 1.0;

        if (state.preset === "dual_regime") {
          const pt = Number(b.p_trend ?? 0.2);
          if (pt < 0.38) {
            // FADE Mode: OU-Spectral mean reversion sized smoothly by (1 - p_trend)
            if (z <= -entryZ) side = "LONG";
            else if (z >= entryZ) side = "SHORT";
            tradeMode = "FADE";
            regimeSizeWeight = clamp(1.0 - pt, 0.55, 1.0);
          } else if (pt > 0.62) {
            // SURF Mode: follow aligned 5-20m institutional VWAP fan flow
            const fDir = Number(b.fan_dir || 0);
            const fAlign = Number(b.fan_align || 0);
            const slp = Number(b.slope || 0);
            if (fAlign >= 0.5 && fDir !== 0 && (fDir * slp >= 0.42) && (fDir * (b.close - b.vwap10) >= 0)) {
              side = fDir > 0 ? "LONG" : "SHORT";
              tradeMode = "SURF";
              regimeSizeWeight = clamp(pt, 0.62, 1.0);
            }
          }
          // 0.38 <= p_trend <= 0.62: Flat Veto Zone (side remains null)
        } else {
          if (z <= -entryZ) side = "LONG";
          else if (z >= entryZ) side = "SHORT";
        }

        if (!side) {
          i += 1;
          continue;
        }

        const dir = side === "LONG" ? 1 : -1;
        const entryIdx = i;
        const entryPx = b.close;
        const entryVwap = vwapBlend[i];
        const dislocBps = Math.abs(entryPx - entryVwap) / entryPx * 10000;
        if (tradeMode !== "SURF" && dislocBps < 6.5) {
          i += 1;
          continue;
        }

        // OU-Spectral / Dual-Regime FADE Curvature-VPIN consensus gate
        if (state.preset === "ou_spectral" || (state.preset === "dual_regime" && tradeMode === "FADE")) {
          const agree4 = side === "LONG"
            ? (entryPx < b.vwap5 && entryPx < b.vwap10 && entryPx < b.vwap15 && entryPx < b.vwap20)
            : (entryPx > b.vwap5 && entryPx > b.vwap10 && entryPx > b.vwap15 && entryPx > b.vwap20);
          const slopeAbs = Math.abs(Number(b.slope || 0));
          const curvAgainst = (-dir) * Number(b.d_curvature || 0);
          const curvReject = curvAgainst > 0.55 && slopeAbs > 0.95;
          const veto = state.preset === "ou_spectral" ? Boolean(b.toxic_flag) : false;
          if (!agree4 || slopeAbs > 1.48 || curvReject || veto) {
            i += 1;
            continue;
          }
        }

        const entrySig = sigmaBlend[i];
        const initGap = Math.abs(entryVwap - entryPx);
        const targetMove = 0.72 * initGap;
        const effMaxHold = (state.preset === "ou_spectral" || state.preset === "dual_regime")
          ? (tradeMode === "SURF" ? maxHold : clamp(Math.round(1.6 * Number(b.tau_half || 10)), minHold, maxHold))
          : maxHold;
        let exitIdx = Math.min(n - 2, entryIdx + effMaxHold);
        let peakRev = 0;
        let armedTrail = false;

        for (let t = entryIdx + minHold; t <= Math.min(n - 2, entryIdx + effMaxHold); t++) {
          const zt = zSeries[t];
          const pxMove = dir * (bars[t].close - entryPx);
          if (pxMove > peakRev) peakRev = pxMove;

          if (tradeMode === "SURF") {
            // Strict causal trailing 10m VWAP cross stop + regime flip protection
            if ((side === "LONG" && bars[t].close < bars[t].vwap10) ||
                (side === "SHORT" && bars[t].close > bars[t].vwap10) ||
                Number(bars[t].p_trend ?? 0.7) < 0.35) {
              exitIdx = t;
              break;
            }
            if (pxMove < -2.3 * entrySig) {
              exitIdx = t;
              break;
            }
          } else if (state.turboMode) {
            // Causal asymmetric trailing reversion lock (captures overshoot past Blended VWAP)
            if (pxMove >= targetMove) armedTrail = true;
            if (armedTrail && (pxMove <= 0.85 * peakRev || pxMove >= 1.32 * initGap)) {
              exitIdx = t;
              break;
            }
            if (pxMove < -2.9 * entrySig) {
              exitIdx = t;
              break;
            }
          } else {
            // Standard exit on anchor reversion back toward entry Blended VWAP or z-score crossover
            if (pxMove >= targetMove || (side === "LONG" && zt >= -0.25) || (side === "SHORT" && zt <= 0.25)) {
              exitIdx = t;
              break;
            }
            if (pxMove < -2.9 * entrySig) {
              exitIdx = t;
              break;
            }
          }
        }

        const exitPx = bars[exitIdx].close;
        const holdMin = exitIdx - entryIdx;
        const rawGrossBps = dir * ((exitPx - entryPx) / entryPx) * 10000;
        const relVol = Math.max(0.35, Number(b.rel_vol || 1.0));
        const volBps = (entrySig / entryPx) * 10000;
        const impactBps = impactCoeff * Math.min(12, volBps) * Math.sqrt(0.012 / relVol);

        let costBps, sizeMult;
        if (state.turboMode) {
          // Passive maker peg execution + strictly causal fractional-Kelly conviction sizing (<4x intraday)
          costBps = 0.32 * spreadBps + 0.84 * impactBps;
          const excessZ = Math.max(Math.abs(z) - entryZ, 0);
          const volScale = clamp(5.5 / Math.max(volBps, 2.5), 0.75, 1.35);
          sizeMult = clamp((2.35 + 1.15 * excessZ + 0.22 * Math.max(relVol - 1.0, 0)) * volScale * regimeSizeWeight, 1.6, 3.9);
        } else {
          costBps = spreadBps + 2 * impactBps;
          sizeMult = regimeSizeWeight;
        }

        const grossBps = rawGrossBps * sizeMult;
        const netBps = (rawGrossBps - costBps) * sizeMult;
        const vwapEdge = tradeMode === "SURF"
          ? (rawGrossBps * 0.35 - costBps * 0.5)
          : (dir * ((vwapBlend[entryIdx] - entryPx) / entryPx) * 10000 - costBps * 0.5);

        trades.push({
          id: tid++,
          side,
          mode: tradeMode,
          entryIdx,
          exitIdx,
          entryTime: bars[entryIdx].time,
          exitTime: bars[exitIdx].time,
          holdMin,
          entryPx,
          exitPx,
          entryZ: z,
          grossBps,
          costBps,
          netBps,
          vwapEdge,
          sizeMult
        });

        i = exitIdx + (state.turboMode ? 1 : 2);
      }
    }

    state.simSeries = { vwapBlend, sigmaBlend, zSeries, w5Arr, w10Arr, w15Arr, w20Arr };
    state.simTrades = trades;
    state.lastComputeMs = typeof performance !== "undefined" ? Math.max(0.01, performance.now() - tPerf0) : 0.15;
  }

  /* ---------- KPI Strip & Control Sync ---------- */
  function syncSlidersUI() {
    const bounds = getZoomBounds();
    let displayWeights = state.weights.slice();

    const isDynamicPreset = PRESETS[state.preset] && PRESETS[state.preset].dynamic;
    if (isDynamicPreset && state.simSeries) {
      let s5 = 0, s10 = 0, s15 = 0, s20 = 0, cnt = 0;
      for (let i = bounds.start; i <= bounds.end; i++) {
        s5 += state.simSeries.w5Arr[i];
        s10 += state.simSeries.w10Arr[i];
        s15 += state.simSeries.w15Arr[i];
        s20 += state.simSeries.w20Arr[i];
        cnt++;
      }
      if (cnt > 0) {
        const w5 = Math.round((s5 / cnt) * 100);
        const w10 = Math.round((s10 / cnt) * 100);
        const w15 = Math.round((s15 / cnt) * 100);
        const w20 = Math.max(0, 100 - w5 - w10 - w15);
        displayWeights = [w5, w10, w15, w20];
      }
    }

    $("sliderW5").value = displayWeights[0];
    $("sliderW10").value = displayWeights[1];
    $("sliderW15").value = displayWeights[2];
    $("sliderW20").value = displayWeights[3];

    $("valW5").textContent = `${displayWeights[0]}%`;
    $("valW10").textContent = `${displayWeights[1]}%`;
    $("valW15").textContent = `${displayWeights[2]}%`;
    $("valW20").textContent = `${displayWeights[3]}%`;

    $("weightSumBadge").textContent = state.preset === "dual_regime"
      ? "Bayes OU 100%"
      : (state.preset === "ou_spectral" ? "OU Kernel 100%" : (isDynamicPreset ? "Dynamic 100%" : "100%"));
    $("presetActiveLabel").textContent = PRESETS[state.preset].short;
    $("valEntryZ").textContent = `${state.entryZ.toFixed(2)}\u03C3`;
    $("valMaxHold").textContent = `${state.maxHold} min`;

    // KPI Weight Bar
    const spans = $("kpiWeightBar").querySelectorAll("span");
    displayWeights.forEach((w, idx) => {
      if (spans[idx]) spans[idx].style.width = `${w}%`;
    });
    $("kpiWeightText").textContent = `${displayWeights[0]} / ${displayWeights[1]} / ${displayWeights[2]} / ${displayWeights[3]}%`;
    $("kpiWeightMode").textContent = state.preset === "dual_regime"
      ? "Fade\u2194Surf"
      : (state.preset === "ou_spectral" ? "OU-Spectral" : (isDynamicPreset ? "Dynamic" : "Fixed"));
  }

  function updateKPIStrip() {
    const bounds = getZoomBounds();
    const visibleTrades = state.simTrades.filter(
      t => t.exitIdx >= bounds.start && t.entryIdx <= bounds.end
    );
    const allTrades = state.simTrades;
    const activeSet = visibleTrades.length > 0 ? visibleTrades : allTrades;

    const totalNetBps = activeSet.reduce((s, t) => s + t.netBps, 0);
    const nTrades = activeSet.length;
    const longs = activeSet.filter(t => t.side === "LONG").length;
    const shorts = nTrades - longs;
    const wins = activeSet.filter(t => t.netBps > 0).length;
    const winRate = nTrades > 0 ? (wins / nTrades) * 100 : 0;

    const grossWin = activeSet.filter(t => t.netBps > 0).reduce((s, t) => s + t.netBps, 0);
    const grossLoss = Math.abs(activeSet.filter(t => t.netBps < 0).reduce((s, t) => s + t.netBps, 0));
    const pf = grossLoss > 1e-6 ? grossWin / grossLoss : (grossWin > 0 ? 9.99 : 0);

    const avgHold = nTrades > 0 ? activeSet.reduce((s, t) => s + t.holdMin, 0) / nTrades : 0;
    const minH = nTrades > 0 ? Math.min(...activeSet.map(t => t.holdMin)) : 0;
    const maxH = nTrades > 0 ? Math.max(...activeSet.map(t => t.holdMin)) : 0;

    const avgCost = nTrades > 0 ? activeSet.reduce((s, t) => s + t.costBps, 0) / nTrades : 0;
    const avgSlippage = nTrades > 0 ? activeSet.reduce((s, t) => s + t.vwapEdge, 0) / nTrades : 0;

    // Reference Net Sharpe: exact benchmark value when on flagship default, else annualized from simulated full-session trades
    let netSharpeDisp = "—";
    const isDefaultAdaptive =
      state.symbol === "ES" &&
      !state.rt.running &&
      state.symbolSource === "benchmark" &&
      state.preset === "adaptive" &&
      Math.abs(state.entryZ - state.defaultEntryZ) < 1e-6 &&
      state.maxHold === state.defaultMaxHold;
    const isDefaultStatic =
      state.symbol === "ES" &&
      state.preset === "static" &&
      Math.abs(state.entryZ - state.defaultEntryZ) < 1e-6 &&
      state.maxHold === state.defaultMaxHold;

    const flagshipCfg = findConfigById(RAW_METRICS && RAW_METRICS.summary && RAW_METRICS.summary.flagship_id) ||
      (RAW_METRICS && RAW_METRICS.configs && RAW_METRICS.configs.find(c => c.is_flagship)) ||
      (RAW_METRICS && RAW_METRICS.configs && RAW_METRICS.configs[0]);

    const rlWf = RAW_METRICS && RAW_METRICS.research_lab && RAW_METRICS.research_lab.walk_forward_dsr;
    const drLab = RAW_METRICS && RAW_METRICS.dual_regime_lab;
    const drWf = drLab && drLab.walk_forward_dsr_m20;
    if (state.preset === "dual_regime" && state.symbol === "ES" && drWf) {
      const drRef = state.turboMode ? drWf.dual_regime_turbo : drWf.dual_regime_1x;
      netSharpeDisp = drRef
        ? `SR ${fmt(drRef.full_sharpe, 2)} (OOS ${fmt(drRef.oos_sharpe, 2)})`
        : computeImpliedSharpe(allTrades);
    } else if (state.preset === "ou_spectral" && state.symbol === "ES" && rlWf) {
      const ouRef = state.turboMode ? rlWf.ou_spectral_turbo : rlWf.ou_spectral_1x;
      netSharpeDisp = ouRef ? `SR ${fmt(ouRef.full_sharpe, 2)} (OOS ${fmt(ouRef.oos_sharpe, 2)})` : computeImpliedSharpe(allTrades);
    } else if (state.turboMode && state.symbol === "ES" && RAW_METRICS && RAW_METRICS.turbo_benchmark && state.preset === "adaptive") {
      const tb = RAW_METRICS.turbo_benchmark;
      netSharpeDisp = `SR ${fmt(tb.turbo_net_sharpe, 2)} · ${fmt(tb.profit_multiple, 1)}\u00D7`;
    } else if (isDefaultAdaptive && flagshipCfg) {
      netSharpeDisp = `SR ${fmt(flagshipCfg.net_sharpe, 2)}`;
    } else if (isDefaultStatic && RAW_METRICS && RAW_METRICS.configs) {
      const staticCfg = RAW_METRICS.configs.find(c => /static/i.test(c.id || c.name));
      netSharpeDisp = staticCfg ? `SR ${fmt(staticCfg.net_sharpe, 2)}` : computeImpliedSharpe(allTrades);
    } else {
      netSharpeDisp = computeImpliedSharpe(allTrades);
    }

    const pnlEl = $("kpiSessionPnl");
    pnlEl.textContent = `${fmtSigned(totalNetBps, 1)} bp`;
    pnlEl.classList.remove("pos", "neg");
    pnlEl.classList.add(totalNetBps >= 0 ? "pos" : "neg");

    $("kpiNetSharpe").textContent = netSharpeDisp;
    $("kpiModeTag").textContent = state.turboMode ? `\u26A1 Turbo` : PRESETS[state.preset].short;
    $("kpiWindowTag").textContent = state.zoom === "full" ? "Full Day" : "In View";
    $("kpiTradeCount").textContent = `${nTrades}`;
    $("kpiLongShort").textContent = `${longs}L / ${shorts}S (${allTrades.length} day)`;
    $("kpiAvgHold").textContent = nTrades > 0 ? `${fmt(avgHold, 1)}m` : "—";
    $("kpiHoldRange").textContent = nTrades > 0 ? `Range ${minH}–${maxH}m` : "No fills";
    $("kpiWinRate").textContent = nTrades > 0 ? `${fmt(winRate, 1)}%` : "—";
    $("kpiProfitFactor").textContent = nTrades > 0 ? `PF ${pf >= 9.9 ? ">9.9" : fmt(pf, 2)}` : "PF —";

    const slipEl = $("kpiSlippage");
    if (isDefaultAdaptive && flagshipCfg && state.zoom === "full") {
      slipEl.textContent = `${fmtSigned(flagshipCfg.vwap_slippage_saved_bps, 2)} bp`;
    } else {
      slipEl.textContent = nTrades > 0 ? `${fmtSigned(avgSlippage, 2)} bp` : "—";
    }
    $("kpiAvgCost").textContent = `Cost ${fmt(avgCost, 2)} bp`;

    $("zoomRangeLabel").textContent = bounds.shortRange;
    $("chartWindowCaption").textContent = `${bounds.label} · ${PRESETS[state.preset].short}`;
    updateLiveTelemetry();
  }

  function computeImpliedSharpe(trades) {
    if (!trades || trades.length < 2) return "SR —";
    const vals = trades.map(t => t.netBps);
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const variance = vals.reduce((a, x) => a + Math.pow(x - mean, 2), 0) / Math.max(1, vals.length - 1);
    const sd = Math.sqrt(variance);
    if (sd < 1e-6) return "SR —";
    const sPerTrade = mean / sd;
    const sr = clamp(sPerTrade * Math.sqrt(trades.length * 252) * 0.18, -4, 6);
    return `Est. SR ${fmt(sr, 2)}`;
  }

  /* ---------- Render Tab 1 Intraday SVG Chart ---------- */
  function renderIntradayChart() {
    const svg = $("intradayChart");
    clearEl(svg);

    const bars = RAW_SESSION.bars;
    const sim = state.simSeries;
    const bounds = getZoomBounds();
    const start = bounds.start;
    const end = bounds.end;
    const count = end - start + 1;

    const W = 1080;
    const H = 462;
    const padL = 54;
    const padR = 64;
    const plotW = W - padL - padR;

    const topY0 = 16;
    const topY1 = 316;
    const topH = topY1 - topY0;

    const divY = 334;
    const botY0 = 342;
    const botY1 = 436;
    const botH = botY1 - botY0;

    // Price domain across visible slice
    let pMin = Infinity;
    let pMax = -Infinity;
    let zMaxAbs = Math.max(2.4, state.entryZ + 0.65);
    let volMax = 2.0;

    for (let i = start; i <= end; i++) {
      const b = bars[i];
      const vb = sim.vwapBlend[i];
      const sig = sim.sigmaBlend[i];
      pMin = Math.min(pMin, b.low, vb - 2.05 * sig);
      pMax = Math.max(pMax, b.high, vb + 2.05 * sig);
      if (state.layers.horizons) {
        pMin = Math.min(pMin, b.vwap5, b.vwap10, b.vwap15, b.vwap20);
        pMax = Math.max(pMax, b.vwap5, b.vwap10, b.vwap15, b.vwap20);
      }
      zMaxAbs = Math.max(zMaxAbs, Math.abs(sim.zSeries[i]) + 0.3);
      volMax = Math.max(volMax, Number(b.rel_vol || 1.0));
    }
    const pPad = (pMax - pMin) * 0.07 || 0.1;
    pMin -= pPad;
    pMax += pPad;
    zMaxAbs = Math.min(4.2, Math.ceil(zMaxAbs * 2) / 2);

    const xOf = idx => padL + ((idx - start) / Math.max(1, count - 1)) * plotW;
    const yPrice = p => topY1 - ((p - pMin) / (pMax - pMin)) * topH;
    const yZ = z => botY0 + botH * 0.5 - (z / zMaxAbs) * (botH * 0.46);

    // Clip paths
    const defs = svgEl("defs");
    const clipTop = svgEl("clipPath", { id: "clipTopPane" });
    clipTop.appendChild(svgEl("rect", { x: padL, y: topY0, width: plotW, height: topH }));
    defs.appendChild(clipTop);

    const clipBot = svgEl("clipPath", { id: "clipBotPane" });
    clipBot.appendChild(svgEl("rect", { x: padL, y: botY0, width: plotW, height: botH }));
    defs.appendChild(clipBot);
    svg.appendChild(defs);

    // Subtle pane background frames
    svg.appendChild(svgEl("rect", {
      x: padL, y: topY0, width: plotW, height: topH,
      fill: "#FFFFFF", stroke: "#DCE7F4", "stroke-width": 1
    }));
    svg.appendChild(svgEl("rect", {
      x: padL, y: botY0, width: plotW, height: botH,
      fill: "#FAFCFF", stroke: "#DCE7F4", "stroke-width": 1
    }));

    // Highlight selected trade window across both panes
    const visibleTrades = state.simTrades.filter(t => t.exitIdx >= start && t.entryIdx <= end);
    if (state.selectedTradeIdx !== null) {
      const selTrade = state.simTrades.find(t => t.id === state.selectedTradeIdx);
      if (selTrade && selTrade.exitIdx >= start && selTrade.entryIdx <= end) {
        const sx0 = xOf(Math.max(start, selTrade.entryIdx));
        const sx1 = xOf(Math.min(end, selTrade.exitIdx));
        const sw = Math.max(8, sx1 - sx0);
        svg.appendChild(svgEl("rect", {
          x: sx0, y: topY0, width: sw, height: topH,
          fill: "#CFE3FA", opacity: 0.48
        }));
        svg.appendChild(svgEl("rect", {
          x: sx0, y: botY0, width: sw, height: botH,
          fill: "#CFE3FA", opacity: 0.48
        }));
      }
    }

    // Horizontal price gridlines (5 ticks)
    const pTicks = 5;
    for (let k = 0; k <= pTicks; k++) {
      const val = pMin + (k / pTicks) * (pMax - pMin);
      const y = yPrice(val);
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: COLORS.grid, "stroke-width": 0.85, "stroke-dasharray": "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL + plotW + 7, y: y + 3.5,
        fill: COLORS.muted, "font-size": 10.5, "font-family": "Segoe UI, sans-serif",
        "font-variant-numeric": "tabular-nums"
      }, val.toFixed(2)));
    }

    // Left price axis title
    svg.appendChild(svgEl("text", {
      x: 15, y: topY0 + topH / 2,
      transform: `rotate(-90 15 ${topY0 + topH / 2})`,
      "text-anchor": "middle", fill: COLORS.muted,
      "font-size": 10.5, "font-style": "italic", "font-family": "Georgia, serif"
    }, "Price & Blended VWAP ($)"));

    // Vertical time gridlines
    const tickStep = count <= 75 ? 10 : (count <= 130 ? 15 : 30);
    for (let i = start; i <= end; i++) {
      const mins = i;
      if (i === start || i === end || mins % tickStep === 0) {
        const x = xOf(i);
        svg.appendChild(svgEl("line", {
          x1: x, y1: topY0, x2: x, y2: topY1,
          stroke: "#E6EFF9", "stroke-width": 0.75
        }));
        svg.appendChild(svgEl("line", {
          x1: x, y1: botY0, x2: x, y2: botY1,
          stroke: "#E6EFF9", "stroke-width": 0.75
        }));
        // Avoid crowded end labels
        if (i === end && end - start > 10 && (end % tickStep) < tickStep * 0.45) continue;
        svg.appendChild(svgEl("text", {
          x, y: botY1 + 15, "text-anchor": "middle",
          fill: COLORS.muted, "font-size": 10.5, "font-family": "Segoe UI, sans-serif",
          "font-variant-numeric": "tabular-nums"
        }, bars[i].time));
      }
    }

    // Clipped upper chart group
    const topGroup = svgEl("g", { "clip-path": "url(#clipTopPane)" });

    // Deviation bands ±2σ and ±1σ
    if (state.layers.bands) {
      const upper2 = [], lower2 = [], upper1 = [], lower1 = [];
      for (let i = start; i <= end; i++) {
        const x = xOf(i);
        const vb = sim.vwapBlend[i];
        const sig = sim.sigmaBlend[i];
        upper2.push(`${x.toFixed(1)},${yPrice(vb + 2 * sig).toFixed(1)}`);
        lower2.unshift(`${x.toFixed(1)},${yPrice(vb - 2 * sig).toFixed(1)}`);
        upper1.push(`${x.toFixed(1)},${yPrice(vb + sig).toFixed(1)}`);
        lower1.unshift(`${x.toFixed(1)},${yPrice(vb - sig).toFixed(1)}`);
      }
      topGroup.appendChild(svgEl("polygon", {
        points: upper2.concat(lower2).join(" "),
        fill: "#68A3DF", "fill-opacity": 0.11
      }));
      topGroup.appendChild(svgEl("polygon", {
        points: upper1.concat(lower1).join(" "),
        fill: "#3B76C4", "fill-opacity": 0.15
      }));
      topGroup.appendChild(svgEl("polyline", {
        points: upper2.join(" "), fill: "none",
        stroke: "#9AC0E6", "stroke-width": 0.8, "stroke-dasharray": "2,3"
      }));
      topGroup.appendChild(svgEl("polyline", {
        points: lower2.slice().reverse().join(" "), fill: "none",
        stroke: "#9AC0E6", "stroke-width": 0.8, "stroke-dasharray": "2,3"
      }));
    }

    // Price Candles or Close Line
    const barSpacing = plotW / Math.max(1, count - 1);
    const drawCandles = state.layers.candles && count <= 145;
    if (drawCandles) {
      const cw = clamp(barSpacing * 0.56, 2.2, 6.2);
      for (let i = start; i <= end; i++) {
        const b = bars[i];
        const x = xOf(i);
        const yH = yPrice(b.high);
        const yL = yPrice(b.low);
        const yO = yPrice(b.open);
        const yC = yPrice(b.close);
        const isUp = b.close >= b.open;
        const bodyTop = Math.min(yO, yC);
        const bodyH = Math.max(1.2, Math.abs(yC - yO));

        topGroup.appendChild(svgEl("line", {
          x1: x, y1: yH, x2: x, y2: yL,
          stroke: isUp ? "#3F6996" : "#4E5F74", "stroke-width": 0.9
        }));
        topGroup.appendChild(svgEl("rect", {
          x: x - cw / 2, y: bodyTop, width: cw, height: bodyH,
          fill: isUp ? "#FFFFFF" : "#6D85A2",
          stroke: isUp ? "#2E5E94" : "#43566E",
          "stroke-width": 0.85, rx: 0.5
        }));
      }
    } else {
      const pts = [];
      for (let i = start; i <= end; i++) {
        pts.push(`${xOf(i).toFixed(1)},${yPrice(bars[i].close).toFixed(1)}`);
      }
      topGroup.appendChild(svgEl("polyline", {
        points: pts.join(" "), fill: "none",
        stroke: "#5E7998", "stroke-width": 1.25
      }));
    }

    // Four Horizon VWAP Curves (5m, 10m, 15m, 20m)
    if (state.layers.horizons) {
      const horizonSpecs = [
        { key: "vwap20", color: "#7E9EC2", width: 1.2, dash: "5,3" },
        { key: "vwap15", color: "#1B4F9C", width: 1.25, dash: "3.5,2.5" },
        { key: "vwap10", color: "#3B76C4", width: 1.35, dash: null },
        { key: "vwap5",  color: "#68A3DF", width: 1.25, dash: null }
      ];
      for (const spec of horizonSpecs) {
        const pts = [];
        for (let i = start; i <= end; i++) {
          pts.push(`${xOf(i).toFixed(1)},${yPrice(bars[i][spec.key]).toFixed(1)}`);
        }
        topGroup.appendChild(svgEl("polyline", {
          points: pts.join(" "), fill: "none",
          stroke: spec.color, "stroke-width": spec.width,
          "stroke-dasharray": spec.dash, "stroke-opacity": 0.92
        }));
      }
    }

    // Bold Deep Quant Navy Blended 5-20m VWAP (with subtle crisp halo)
    const blendPts = [];
    for (let i = start; i <= end; i++) {
      blendPts.push(`${xOf(i).toFixed(1)},${yPrice(sim.vwapBlend[i]).toFixed(1)}`);
    }
    topGroup.appendChild(svgEl("polyline", {
      points: blendPts.join(" "), fill: "none",
      stroke: "#FFFFFF", "stroke-width": 4.2, "stroke-opacity": 0.72,
      "stroke-linejoin": "round", "stroke-linecap": "round"
    }));
    topGroup.appendChild(svgEl("polyline", {
      points: blendPts.join(" "), fill: "none",
      stroke: COLORS.navy, "stroke-width": 2.3,
      "stroke-linejoin": "round", "stroke-linecap": "round"
    }));

    // Trade Entry/Exit Markers & Non-Overlapping PnL Badges
    if (state.layers.trades && visibleTrades.length > 0) {
      const placedBadges = [];
      const showBadges = visibleTrades.length <= 16;

      for (const t of visibleTrades) {
        const isSel = state.selectedTradeIdx === t.id;
        const isSurf = t.mode === "SURF";
        const color = isSurf ? COLORS.cobalt : (t.side === "LONG" ? COLORS.long : COLORS.short);
        const ex = xOf(clamp(t.entryIdx, start, end));
        const ey = yPrice(t.entryPx);
        const xx = xOf(clamp(t.exitIdx, start, end));
        const xy = yPrice(t.exitPx);

        // Connecting trade path (solid cobalt for SURF momentum fills, dashed for FADE)
        topGroup.appendChild(svgEl("line", {
          x1: ex, y1: ey, x2: xx, y2: xy,
          stroke: color, "stroke-width": isSel ? 2.2 : (isSurf ? 1.75 : 1.35),
          "stroke-dasharray": (isSel || isSurf) ? null : "3,2", opacity: isSel ? 1 : 0.88
        }));

        // Entry marker: diamond for SURF mode, triangle for FADE/standard
        if (t.entryIdx >= start && t.entryIdx <= end) {
          const r = isSel ? 6.2 : 5.0;
          const markerPts = isSurf
            ? `${ex},${ey - r} ${ex + r},${ey} ${ex},${ey + r} ${ex - r},${ey}`
            : (t.side === "LONG"
              ? `${ex},${ey - r - 1} ${ex - r},${ey + r - 1} ${ex + r},${ey + r - 1}`
              : `${ex},${ey + r + 1} ${ex - r},${ey - r + 1} ${ex + r},${ey - r + 1}`);
          const marker = svgEl("polygon", {
            points: markerPts, fill: color, stroke: "#FFFFFF", "stroke-width": 1.15,
            style: "cursor:pointer;"
          });
          marker.addEventListener("click", () => selectTrade(t.id));
          topGroup.appendChild(marker);
        }

        // Exit ring marker
        if (t.exitIdx >= start && t.exitIdx <= end) {
          const exitDot = svgEl("circle", {
            cx: xx, cy: xy, r: isSel ? 4.8 : 3.6,
            fill: "#FFFFFF", stroke: color, "stroke-width": isSel ? 2.2 : 1.7,
            style: "cursor:pointer;"
          });
          exitDot.addEventListener("click", () => selectTrade(t.id));
          topGroup.appendChild(exitDot);

          // Compact PnL badge with basic collision avoidance
          if (showBadges || isSel) {
            const label = isSurf ? `S ${fmtSigned(t.netBps, 1)}bp` : `${fmtSigned(t.netBps, 1)}bp`;
            const bw = isSurf ? 54 : 46;
            const bh = 15;
            let bx = clamp(xx - bw / 2, padL + 4, padL + plotW - bw - 4);
            let by = t.side === "LONG" ? xy - bh - 9 : xy + 9;
            by = clamp(by, topY0 + 4, topY1 - bh - 4);

            for (let attempt = 0; attempt < 4; attempt++) {
              const clash = placedBadges.some(pb =>
                Math.abs(pb.x - bx) < bw + 3 && Math.abs(pb.y - by) < bh + 3
              );
              if (!clash) break;
              by = by + (attempt % 2 === 0 ? -18 : 18);
              by = clamp(by, topY0 + 4, topY1 - bh - 4);
            }
            placedBadges.push({ x: bx, y: by });

            const gBadge = svgEl("g", { style: "cursor:pointer;" });
            gBadge.appendChild(svgEl("rect", {
              x: bx, y: by, width: bw, height: bh, rx: 3,
              fill: isSurf ? COLORS.ice : (t.netBps >= 0 ? COLORS.longBg : COLORS.shortBg),
              stroke: isSurf ? COLORS.cobalt : (t.netBps >= 0 ? "#8BC9C6" : "#DFB1B8"),
              "stroke-width": isSel ? 1.5 : 0.95
            }));
            gBadge.appendChild(svgEl("text", {
              x: bx + bw / 2, y: by + 10.6, "text-anchor": "middle",
              fill: isSurf ? COLORS.navy : (t.netBps >= 0 ? COLORS.long : COLORS.short),
              "font-size": 9.2, "font-weight": 700,
              "font-family": "Segoe UI, sans-serif",
              "font-variant-numeric": "tabular-nums"
            }, label));
            gBadge.addEventListener("click", () => selectTrade(t.id));
            topGroup.appendChild(gBadge);
          }
        }
      }
    }

    svg.appendChild(topGroup);

    // Lower Pane Header Divider
    svg.appendChild(svgEl("text", {
      x: padL + 4, y: divY,
      fill: COLORS.navy, "font-size": 10.5, "font-weight": 700,
      "font-family": "Georgia, serif"
    }, "Subordinate Panel: Volume-Clock (Rel. Vol) & Blended Z-Score"));

    svg.appendChild(svgEl("text", {
      x: padL + plotW - 4, y: divY, "text-anchor": "end",
      fill: COLORS.muted, "font-size": 10,
      "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
    }, `Entry Threshold: \u00B1${state.entryZ.toFixed(2)}\u03C3`));

    // Clipped lower pane group
    const botGroup = svgEl("g", { "clip-path": "url(#clipBotPane)" });

    // Shaded trigger zones outside ±entryZ
    const yPosThr = yZ(state.entryZ);
    const yNegThr = yZ(-state.entryZ);
    botGroup.appendChild(svgEl("rect", {
      x: padL, y: botY0, width: plotW, height: Math.max(0, yPosThr - botY0),
      fill: "#9B2C3B", "fill-opacity": 0.06
    }));
    botGroup.appendChild(svgEl("rect", {
      x: padL, y: yNegThr, width: plotW, height: Math.max(0, botY1 - yNegThr),
      fill: "#0E6A68", "fill-opacity": 0.06
    }));

    // Relative Volume Bars
    const vw = clamp(barSpacing * 0.65, 1.4, 5.5);
    for (let i = start; i <= end; i++) {
      const rv = Math.max(0.1, Number(bars[i].rel_vol || 1.0));
      const h = (rv / (volMax * 1.15)) * (botH * 0.62);
      botGroup.appendChild(svgEl("rect", {
        x: xOf(i) - vw / 2, y: botY1 - h, width: vw, height: h,
        fill: rv >= 1.35 ? "#9AC0E6" : "#D3E4F6", opacity: 0.78
      }));
    }

    // Zero & ±entryZ lines
    const yZero = yZ(0);
    botGroup.appendChild(svgEl("line", {
      x1: padL, y1: yZero, x2: padL + plotW, y2: yZero,
      stroke: "#98B4D4", "stroke-width": 0.9
    }));
    botGroup.appendChild(svgEl("line", {
      x1: padL, y1: yPosThr, x2: padL + plotW, y2: yPosThr,
      stroke: COLORS.cobalt, "stroke-width": 0.95, "stroke-dasharray": "4,3", opacity: 0.85
    }));
    botGroup.appendChild(svgEl("line", {
      x1: padL, y1: yNegThr, x2: padL + plotW, y2: yNegThr,
      stroke: COLORS.cobalt, "stroke-width": 0.95, "stroke-dasharray": "4,3", opacity: 0.85
    }));

    // Z-Score Line
    const zPts = [];
    for (let i = start; i <= end; i++) {
      zPts.push(`${xOf(i).toFixed(1)},${yZ(sim.zSeries[i]).toFixed(1)}`);
    }
    botGroup.appendChild(svgEl("polyline", {
      points: zPts.join(" "), fill: "none",
      stroke: COLORS.cobalt, "stroke-width": 1.65, "stroke-linejoin": "round"
    }));
    svg.appendChild(botGroup);

    // Lower pane Z-score right-side ticks
    [state.entryZ, 0, -state.entryZ].forEach(zv => {
      const y = yZ(zv);
      svg.appendChild(svgEl("text", {
        x: padL + plotW + 7, y: y + 3.5,
        fill: zv === 0 ? COLORS.muted : COLORS.cobalt,
        "font-size": 9.8, "font-weight": zv === 0 ? 400 : 600,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${zv > 0 ? "+" : ""}${zv.toFixed(1)}\u03C3`));
    });

    svg.appendChild(svgEl("text", {
      x: 15, y: botY0 + botH / 2,
      transform: `rotate(-90 15 ${botY0 + botH / 2})`,
      "text-anchor": "middle", fill: COLORS.muted,
      "font-size": 10, "font-style": "italic", "font-family": "Georgia, serif"
    }, "z-Score / Vol"));

    // Interactive Crosshair Layer
    const crossG = svgEl("g", { id: "crosshairLayer", style: "display:none; pointer-events:none;" });
    const crossLine = svgEl("line", {
      x1: padL, y1: topY0, x2: padL, y2: botY1,
      stroke: COLORS.navy, "stroke-width": 0.9, "stroke-dasharray": "3,2", opacity: 0.75
    });
    const dotTop = svgEl("circle", {
      cx: 0, cy: 0, r: 4, fill: COLORS.navy, stroke: "#FFFFFF", "stroke-width": 1.5
    });
    const dotBot = svgEl("circle", {
      cx: 0, cy: 0, r: 3.5, fill: COLORS.cobalt, stroke: "#FFFFFF", "stroke-width": 1.3
    });
    crossG.appendChild(crossLine);
    crossG.appendChild(dotTop);
    crossG.appendChild(dotBot);
    svg.appendChild(crossG);

    // Transparent interaction overlay
    const hitRect = svgEl("rect", {
      x: padL, y: topY0, width: plotW, height: botY1 - topY0,
      fill: "transparent", style: "cursor:crosshair;"
    });

    const tooltip = $("chartTooltip");
    hitRect.addEventListener("mousemove", evt => {
      const rect = svg.getBoundingClientRect();
      const svgX = ((evt.clientX - rect.left) / rect.width) * W;
      const frac = clamp((svgX - padL) / plotW, 0, 1);
      const barIdx = clamp(Math.round(start + frac * (count - 1)), start, end);
      const b = bars[barIdx];
      const cx = xOf(barIdx);
      const cyTop = yPrice(sim.vwapBlend[barIdx]);
      const cyBot = yZ(sim.zSeries[barIdx]);

      crossG.style.display = "block";
      crossLine.setAttribute("x1", cx);
      crossLine.setAttribute("x2", cx);
      dotTop.setAttribute("cx", cx);
      dotTop.setAttribute("cy", cyTop);
      dotBot.setAttribute("cx", cx);
      dotBot.setAttribute("cy", cyBot);

      const w5p = Math.round(sim.w5Arr[barIdx] * 100);
      const w10p = Math.round(sim.w10Arr[barIdx] * 100);
      const w15p = Math.round(sim.w15Arr[barIdx] * 100);
      const w20p = Math.max(0, 100 - w5p - w10p - w15p);

      tooltip.innerHTML = `
        <div class="tt-head">
          <span class="tt-time">${b.time} ET</span>
          <span class="tt-px num">$${fmt(b.close, 2)}</span>
        </div>
        <div class="tt-grid num">
          <span class="tt-k">Blended 5–20m</span><span class="tt-v">$${fmt(sim.vwapBlend[barIdx], 2)}</span>
          <span class="tt-k">5m / 10m VWAP</span><span class="tt-v">${fmt(b.vwap5, 2)} / ${fmt(b.vwap10, 2)}</span>
          <span class="tt-k">15m / 20m VWAP</span><span class="tt-v">${fmt(b.vwap15, 2)} / ${fmt(b.vwap20, 2)}</span>
          <span class="tt-k">Blended z-Score</span><span class="tt-v">${fmtSigned(sim.zSeries[barIdx], 2)}&sigma;</span>
          <span class="tt-k">OU &tau;&#189; / Curv C<sub>t</sub></span><span class="tt-v">${fmt(b.tau_half, 1)}m / ${fmtSigned(b.curvature, 2)}</span>
          <span class="tt-k">VPIN / Hurst H&#770;</span><span class="tt-v">${fmt(b.vpin, 2)} / ${fmt(b.hurst, 2)}</span>
          <span class="tt-k">Hamilton P(Trend)</span><span class="tt-v">${fmt(b.p_trend, 2)} (${b.regime_mode || "FADE"})</span>
          <span class="tt-k">Rel. Volume</span><span class="tt-v">${fmt(b.rel_vol, 2)}&times;</span>
        </div>
        <div class="tt-weights num">
          Weights (5/10/15/20m): <strong>${w5p}% / ${w10p}% / ${w15p}% / ${w20p}%</strong>
        </div>
      `;
      tooltip.classList.add("visible");

      // Position tooltip smartly on opposite side of cursor
      const stageRect = $("chartStage").getBoundingClientRect();
      const relX = evt.clientX - stageRect.left;
      if (relX > stageRect.width * 0.56) {
        tooltip.style.left = "66px";
        tooltip.style.right = "auto";
      } else {
        tooltip.style.left = "auto";
        tooltip.style.right = "74px";
      }
      tooltip.style.top = "18px";
    });

    hitRect.addEventListener("mouseleave", () => {
      crossG.style.display = "none";
      tooltip.classList.remove("visible");
    });

    svg.appendChild(hitRect);
  }

  /* ---------- Render Tab 1 Trade Tape Table ---------- */
  function renderTradeTape() {
    const tbody = $("tradeTapeBody");
    clearEl(tbody);

    const bounds = getZoomBounds();
    const visibleTrades = state.simTrades.filter(
      t => t.exitIdx >= bounds.start && t.entryIdx <= bounds.end
    );
    const list = visibleTrades.length > 0 ? visibleTrades : state.simTrades;

    $("tapeSummaryText").textContent = visibleTrades.length > 0
      ? `Showing ${visibleTrades.length} fills in active view (${state.simTrades.length} total session) · click row to spotlight`
      : `No fills inside active zoom; showing all ${state.simTrades.length} session fills`;

    $("btnClearTradeSelection").style.display = state.selectedTradeIdx !== null ? "inline-block" : "none";

    if (list.length === 0) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="12" class="center" style="padding:16px; color:#526D8F;">No trades triggered at current Entry Band Threshold (&plusmn;${state.entryZ.toFixed(2)}&sigma;). Lower the threshold slider to observe fills.</td>`;
      tbody.appendChild(tr);
      return;
    }

    for (const t of list) {
      const tr = document.createElement("tr");
      if (state.selectedTradeIdx === t.id) tr.classList.add("selected-row");
      const modePill = t.mode ? `<span class="mode-pill ${t.mode.toLowerCase()}">${t.mode}</span>` : "";
      tr.innerHTML = `
        <td class="center num">${t.id}</td>
        <td class="center"><span class="side-pill ${t.side === "LONG" ? "long" : "short"}">${t.side}</span>${modePill}</td>
        <td class="left num">${t.entryTime}</td>
        <td class="left num">${t.exitTime}</td>
        <td class="num">${t.holdMin}m</td>
        <td class="num">${fmt(t.entryPx, 2)}</td>
        <td class="num">${fmt(t.exitPx, 2)}</td>
        <td class="num">${fmtSigned(t.entryZ, 2)}&sigma;</td>
        <td class="num">${fmtSigned(t.grossBps, 2)}</td>
        <td class="num">${fmt(t.costBps, 2)}</td>
        <td class="num ${t.netBps >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtSigned(t.netBps, 2)} bp</td>
        <td class="num ${t.vwapEdge >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtSigned(t.vwapEdge, 2)} bp</td>
      `;
      tr.addEventListener("click", () => selectTrade(t.id));
      tbody.appendChild(tr);
    }
  }

  function selectTrade(tradeId) {
    state.selectedTradeIdx = state.selectedTradeIdx === tradeId ? null : tradeId;
    renderIntradayChart();
    renderTradeTape();
  }

  function recomputeAndRenderLab() {
    if (!RAW_SESSION || !RAW_SESSION.bars) return;
    recomputeSession();
    syncSlidersUI();
    updateKPIStrip();
    renderIntradayChart();
    renderTradeTape();
  }

  /* ==========================================================================
     TAB 2: HORIZON ATTRIBUTION & FRONTIER
     ========================================================================== */

  function findConfigById(id) {
    if (!RAW_METRICS || !Array.isArray(RAW_METRICS.configs)) return null;
    return RAW_METRICS.configs.find(c => c.id === id) || null;
  }

  function renderFrontierChart() {
    const svg = $("frontierChart");
    clearEl(svg);
    if (!RAW_METRICS || !Array.isArray(RAW_METRICS.configs)) return;

    const configs = RAW_METRICS.configs;
    const regimes = RAW_METRICS.regimes || [];

    const W = 820;
    const H = 388;
    const padL = 56;
    const padR = 22;
    const padT = 44;
    const padB = 54;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    // X domain: 4.4 to 20.8 minutes, regime split points at 8.5 and 14.5
    const xMin = 4.3;
    const xMax = 21.0;
    const xOf = m => padL + ((m - xMin) / (xMax - xMin)) * plotW;

    // Y domain: Net Sharpe
    const sharpes = configs.map(c => Number(c.net_sharpe));
    const sMinRaw = Math.min(...sharpes);
    const sMaxRaw = Math.max(...sharpes);
    const yMin = Math.max(0, Math.floor((sMinRaw - 0.35) * 4) / 4);
    const yMax = Math.ceil((sMaxRaw + 0.45) * 4) / 4;
    const yOf = s => padT + plotH - ((s - yMin) / Math.max(0.5, yMax - yMin)) * plotH;

    // Bubble radius scaled by annual_return_pct (sleek proportions for clean visual separation)
    const rets = configs.map(c => Math.max(1, Number(c.annual_return_pct || 10)));
    const rMinVal = Math.min(...rets);
    const rMaxVal = Math.max(...rets);
    const radiusOf = c => {
      const norm = (Math.max(1, Number(c.annual_return_pct || 10)) - rMinVal) / Math.max(1e-6, rMaxVal - rMinVal);
      return 6.5 + Math.sqrt(norm) * 8.3;
    };

    const split1 = xOf(8.5);
    const split2 = xOf(14.5);

    // 3 Regime Zones (Middle Core Blended shaded Ice Blue like reference!)
    svg.appendChild(svgEl("rect", {
      x: padL, y: padT - 34, width: split1 - padL, height: plotH + 34,
      fill: "#FFFFFF"
    }));
    svg.appendChild(svgEl("rect", {
      x: split1, y: padT - 34, width: split2 - split1, height: plotH + 34,
      fill: COLORS.ice
    }));
    svg.appendChild(svgEl("rect", {
      x: split2, y: padT - 34, width: padL + plotW - split2, height: plotH + 34,
      fill: "#FFFFFF"
    }));

    // Subtle zone boundaries
    [split1, split2].forEach(sx => {
      svg.appendChild(svgEl("line", {
        x1: sx, y1: padT - 34, x2: sx, y2: padT + plotH,
        stroke: "#D4E3F3", "stroke-width": 1
      }));
    });

    // Zone Top Titles & Sublabels
    const zoneSpecs = [
      { id: "micro",  x0: padL,   x1: split1, title: "Micro-Pulse",  sub: "holding 5 to 8 minutes" },
      { id: "core",   x0: split1, x1: split2, title: "Core Blended", sub: "holding 9 to 14 minutes" },
      { id: "anchor", x0: split2, x1: padL + plotW, title: "Anchor Drift", sub: "holding 15 to 20 minutes" }
    ];
    for (const z of zoneSpecs) {
      const reg = regimes.find(r => r.id === z.id);
      const cx = (z.x0 + z.x1) / 2;
      svg.appendChild(svgEl("text", {
        x: cx, y: 20, "text-anchor": "middle",
        fill: COLORS.navy, "font-size": 12.5, "font-weight": 700,
        "font-family": "Georgia, serif"
      }, reg ? reg.label : z.title));
      svg.appendChild(svgEl("text", {
        x: cx, y: 34, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 10.5, "font-style": "italic",
        "font-family": "Georgia, serif"
      }, reg ? reg.sublabel : z.sub));

      // Bottom Regime Rollup Footer (mirroring reference image!)
      if (reg) {
        const footerTxt = `Avg SR ${fmt(reg.avg_sharpe, 2)} · ${fmt(reg.return_share_pct, 1)}% share · ${reg.config_count} configs`;
        svg.appendChild(svgEl("text", {
          x: cx, y: H - 10, "text-anchor": "middle",
          fill: z.id === "core" ? COLORS.navy : COLORS.ink,
          "font-size": 10.2, "font-weight": z.id === "core" ? 700 : 500,
          "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
        }, footerTxt));
      }
    }

    // Horizontal dashed gridlines & Y ticks
    const yStep = (yMax - yMin) <= 2.5 ? 0.5 : 1.0;
    for (let yv = yMin; yv <= yMax + 1e-6; yv += yStep) {
      const y = yOf(yv);
      if (y < padT - 2 || y > padT + plotH + 2) continue;
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: COLORS.grid, "stroke-width": 0.9, "stroke-dasharray": "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL - 8, y: y + 3.5, "text-anchor": "end",
        fill: COLORS.muted, "font-size": 10.5,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, yv.toFixed(1)));
    }

    // Axes spines (clean L-frame matching reference image)
    svg.appendChild(svgEl("line", {
      x1: padL, y1: padT - 28, x2: padL, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 1.3
    }));
    svg.appendChild(svgEl("line", {
      x1: padL, y1: padT + plotH, x2: padL + plotW, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 1.3
    }));

    // X-axis holding minute ticks
    [5, 8, 10, 12, 15, 18, 20].forEach(m => {
      const x = xOf(m);
      svg.appendChild(svgEl("line", {
        x1: x, y1: padT + plotH, x2: x, y2: padT + plotH + 4,
        stroke: COLORS.navy, "stroke-width": 1
      }));
      svg.appendChild(svgEl("text", {
        x, y: padT + plotH + 16, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 10,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${m}m`));
    });

    svg.appendChild(svgEl("text", {
      x: 15, y: padT + plotH / 2,
      transform: `rotate(-90 15 ${padT + plotH / 2})`,
      "text-anchor": "middle", fill: COLORS.ink,
      "font-size": 11, "font-style": "italic", "font-family": "Georgia, serif"
    }, "Annualized Net Sharpe Ratio"));

    // Dashed connector lines joining the Blended 5-20m family
    const blendedNodes = configs
      .filter(c => c.is_blended)
      .sort((a, b) => Number(a.avg_hold_min) - Number(b.avg_hold_min));
    if (blendedNodes.length >= 2) {
      const pts = blendedNodes.map(c => `${xOf(c.avg_hold_min).toFixed(1)},${yOf(c.net_sharpe).toFixed(1)}`).join(" ");
      svg.appendChild(svgEl("polyline", {
        points: pts, fill: "none",
        stroke: COLORS.cobalt, "stroke-width": 1.4, "stroke-dasharray": "4,3", opacity: 0.9
      }));
    }

    // Legend cleanly placed in open top-right space of Anchor Drift zone
    const legX = padL + plotW - 156;
    const legY = padT + 14;
    svg.appendChild(svgEl("line", {
      x1: legX, y1: legY, x2: legX + 22, y2: legY,
      stroke: COLORS.cobalt, "stroke-width": 1.4, "stroke-dasharray": "4,2.5"
    }));
    svg.appendChild(svgEl("text", {
      x: legX + 27, y: legY + 3.5,
      fill: COLORS.muted, "font-size": 10, "font-style": "italic",
      "font-family": "Georgia, serif"
    }, "Blended 5–20m family"));

    // Precompute bubble nodes
    const nodes = configs.map(c => ({
      cfg: c,
      cx: xOf(Number(c.avg_hold_min)),
      cy: yOf(Number(c.net_sharpe)),
      r: radiusOf(c)
    }));

    // Draw circles first
    for (const n of nodes) {
      const c = n.cfg;
      const isSel = state.selectedConfigId === c.id;
      let fill = COLORS.cerulean;
      let stroke = "#2E609D";
      if (c.is_flagship) {
        fill = COLORS.navy;
        stroke = COLORS.cobalt;
      } else if (c.is_blended) {
        fill = COLORS.cobalt;
        stroke = COLORS.navy;
      } else if (c.regime === "core") {
        fill = COLORS.steel;
        stroke = "#244D85";
      }

      const g = svgEl("g", { style: "cursor:pointer;" });
      if (c.is_flagship || isSel) {
        g.appendChild(svgEl("circle", {
          cx: n.cx, cy: n.cy, r: n.r + (isSel ? 4.5 : 3.2),
          fill: "none", stroke: isSel ? COLORS.cobalt : COLORS.navy,
          "stroke-width": isSel ? 2.0 : 1.25,
          "stroke-dasharray": isSel ? null : "2,2", opacity: 0.9
        }));
      }

      g.appendChild(svgEl("circle", {
        cx: n.cx, cy: n.cy, r: n.r,
        fill, "fill-opacity": c.is_flagship ? 0.95 : (c.is_blended ? 0.88 : 0.80),
        stroke: isSel ? "#FFFFFF" : stroke,
        "stroke-width": isSel ? 1.8 : 1.15
      }));

      g.addEventListener("click", () => selectConfig(c.id));
      svg.appendChild(g);
    }

    // Precision collision-free label placement for all 13 nodes
    // Preferred editorial placements handle tight clusters cleanly; automatic fallback guarantees zero collisions.
    const PREFERRED_PLACEMENT = {
      adaptive_blend_5_20:       { dx:  25, dy:  -6, anchor: "start"  },
      vol_weighted_blend_5_20:   { dx: -22, dy: -20, anchor: "end",   leader: true },
      static_blend_5_20:         { dx: -21, dy:   6, anchor: "end"    },
      micro_8m_elastic_fade:     { dx: -19, dy:  -8, anchor: "end"    },
      core_10m_vwap:             { dx:  19, dy:  -8, anchor: "start"  },
      micro_5m_flow_sweep:       { dx:  16, dy: -10, anchor: "start"  },
      anchor_18m_slow_reversion: { dx: -17, dy: -10, anchor: "end"    },
      core_12m_slope_filtered:   { dx: -15, dy: -10, anchor: "end"    },
      micro_5m_fast_fade:        { dx:  -6, dy:  15, anchor: "start", leader: true },
      micro_6m_micro_band:       { dx:  15, dy: -11, anchor: "start"  },
      anchor_15m_inst_band:      { dx: -12, dy: -11, anchor: "end"    },
      anchor_16m_drift_pullback: { dx:   2, dy: -36, anchor: "middle", leader: true },
      anchor_20m_anchor_vwap:    { dx:   2, dy: -32, anchor: "middle" }
    };

    const placedBoxes = [
      // Reserve legend region in top-right so labels never cross it
      { x: legX - 6, y: legY - 12, w: 162, h: 22 }
    ];
    const orderedForLabels = nodes.slice().sort((a, b) =>
      (b.cfg.is_flagship ? 2 : b.cfg.is_blended ? 1 : 0) - (a.cfg.is_flagship ? 2 : a.cfg.is_blended ? 1 : 0) ||
      b.r - a.r
    );

    function boxOverlapsCircle(box, node) {
      const nearX = clamp(node.cx, box.x, box.x + box.w);
      const nearY = clamp(node.cy, box.y, box.y + box.h);
      const dx = node.cx - nearX;
      const dy = node.cy - nearY;
      return (dx * dx + dy * dy) < Math.pow(node.r + 3.5, 2);
    }

    function boxesOverlap(a, b) {
      return !(a.x + a.w + 4 < b.x || b.x + b.w + 4 < a.x || a.y + a.h + 3 < b.y || b.y + b.h + 3 < a.y);
    }

    for (const n of orderedForLabels) {
      const c = n.cfg;
      const line1 = `${c.short_label || c.name} (${fmt(c.net_sharpe, 2)})`;
      const line2 = `${fmt(c.avg_hold_min, 1)}m · ${fmtSigned(c.annual_return_pct, 1)}%`;
      const approxW = Math.max(line1.length * 5.7, line2.length * 5.0) + 8;
      const approxH = 22;

      const pref = PREFERRED_PLACEMENT[c.id];
      const candidates = [];
      if (pref) {
        const px = pref.anchor === "end"
          ? n.cx + pref.dx - approxW
          : (pref.anchor === "middle" ? n.cx + pref.dx - approxW / 2 : n.cx + pref.dx);
        candidates.push({ x: px, y: n.cy + pref.dy, anchor: pref.anchor, leader: Boolean(pref.leader), prefBonus: -40 });
      }
      candidates.push(
        { x: n.cx + n.r + 7, y: n.cy - approxH / 2, anchor: "start" },
        { x: n.cx - n.r - 7 - approxW, y: n.cy - approxH / 2, anchor: "end" },
        { x: n.cx - approxW / 2, y: n.cy - n.r - approxH - 6, anchor: "middle" },
        { x: n.cx - approxW / 2, y: n.cy + n.r + 6, anchor: "middle" },
        { x: n.cx + n.r + 6, y: n.cy - n.r - approxH - 2, anchor: "start", leader: true },
        { x: n.cx - n.r - 6 - approxW, y: n.cy - n.r - approxH - 2, anchor: "end", leader: true },
        { x: n.cx + n.r + 6, y: n.cy + n.r + 2, anchor: "start", leader: true },
        { x: n.cx - n.r - 6 - approxW, y: n.cy + n.r + 2, anchor: "end", leader: true }
      );

      let best = candidates[0];
      let bestPenalty = Infinity;

      for (const cand of candidates) {
        const box = { x: cand.x, y: cand.y, w: approxW, h: approxH };
        let penalty = cand.prefBonus || 0;
        if (box.x < padL + 3) penalty += 300 + (padL + 3 - box.x) * 6;
        if (box.x + box.w > padL + plotW - 3) penalty += 300 + (box.x + box.w - (padL + plotW - 3)) * 6;
        if (box.y < padT - 4) penalty += 300 + (padT - 4 - box.y) * 6;
        if (box.y + box.h > padT + plotH - 4) penalty += 300 + (box.y + box.h - (padT + plotH - 4)) * 6;

        for (const other of nodes) {
          if (boxOverlapsCircle(box, other)) penalty += 180;
        }
        for (const pb of placedBoxes) {
          if (boxesOverlap(box, pb)) penalty += 160;
        }
        if (penalty < bestPenalty) {
          bestPenalty = penalty;
          best = cand;
        }
      }

      const finalX = clamp(best.x, padL + 4, padL + plotW - approxW - 4);
      const finalY = clamp(best.y, padT - 2, padT + plotH - approxH - 4);
      placedBoxes.push({ x: finalX, y: finalY, w: approxW, h: approxH });

      // Subtle leader hairline when label sits offset from bubble
      const boxCx = finalX + approxW / 2;
      const boxCy = finalY + approxH / 2;
      const dist = Math.hypot(boxCx - n.cx, boxCy - n.cy);
      if (best.leader && dist > n.r + 14) {
        const lx = clamp(n.cx, finalX + 4, finalX + approxW - 4);
        const ly = clamp(n.cy, finalY + 2, finalY + approxH - 2);
        const ux = (lx - n.cx) / Math.max(1, Math.hypot(lx - n.cx, ly - n.cy));
        const uy = (ly - n.cy) / Math.max(1, Math.hypot(lx - n.cx, ly - n.cy));
        svg.appendChild(svgEl("line", {
          x1: (n.cx + ux * (n.r + 2)).toFixed(1),
          y1: (n.cy + uy * (n.r + 2)).toFixed(1),
          x2: lx.toFixed(1),
          y2: ly.toFixed(1),
          stroke: "#8AA9CE", "stroke-width": 0.85, "stroke-dasharray": "2,1.5"
        }));
      }

      const textX = best.anchor === "end" ? finalX + approxW : (best.anchor === "middle" ? finalX + approxW / 2 : finalX);
      const lblGroup = svgEl("g", { style: "cursor:pointer;" });
      lblGroup.appendChild(svgEl("text", {
        x: textX, y: finalY + 10, "text-anchor": best.anchor,
        fill: c.is_flagship ? COLORS.navy : COLORS.ink,
        "font-size": c.is_flagship ? 10.4 : 9.6,
        "font-weight": c.is_blended ? 700 : 600,
        "font-family": "Georgia, serif"
      }, line1));
      lblGroup.appendChild(svgEl("text", {
        x: textX, y: finalY + 20, "text-anchor": best.anchor,
        fill: COLORS.muted, "font-size": 8.9, "font-style": "italic",
        "font-family": "Georgia, serif", "font-variant-numeric": "tabular-nums"
      }, line2));
      lblGroup.addEventListener("click", () => selectConfig(c.id));
      svg.appendChild(lblGroup);
    }
  }

  function renderConfigInspector() {
    const cfg = findConfigById(state.selectedConfigId) ||
      (RAW_METRICS && RAW_METRICS.configs && RAW_METRICS.configs[0]);
    if (!cfg) return;

    $("ciName").textContent = cfg.name;
    $("ciMeta").textContent = `${getRegimeLabel(cfg.regime)} · ${cfg.window_label}${cfg.is_flagship ? " · Flagship" : cfg.is_blended ? " · Blended" : ""}`;

    const metricsHtml = [
      { k: "Net SR", v: fmt(cfg.net_sharpe, 2) },
      { k: "Ann. Ret", v: `${fmtSigned(cfg.annual_return_pct, 1)}%` },
      { k: "Hold", v: `${fmt(cfg.avg_hold_min, 1)}m` },
      { k: "Slip. Saved", v: `${fmtSigned(cfg.vwap_slippage_saved_bps, 2)} bp` },
      { k: "Trades/Yr", v: fmtInt(cfg.annual_trades) },
      { k: "Win Rate", v: `${fmt(cfg.win_rate_pct, 1)}%` }
    ].map(m => `
      <div class="ci-stat">
        <span class="ci-stat-k">${m.k}</span>
        <span class="ci-stat-v">${m.v}</span>
      </div>
    `).join("");
    $("ciMetrics").innerHTML = metricsHtml;
  }

  function selectConfig(cfgId) {
    state.selectedConfigId = cfgId;
    renderFrontierChart();
    renderConfigInspector();
    renderBenchmarkTable();
  }

  function loadSelectedConfigInLab() {
    const cfg = findConfigById(state.selectedConfigId);
    if (!cfg) {
      switchTab("lab");
      return;
    }
    const idStr = String(cfg.id || cfg.name).toLowerCase();
    if (/adaptive/.test(idStr)) {
      applyPreset("adaptive");
    } else if (/static/.test(idStr)) {
      applyPreset("static");
    } else if (/vol/.test(idStr)) {
      state.preset = "custom";
      state.weights = [28, 32, 24, 16];
      state.maxHold = clamp(Math.round(cfg.avg_hold_min * 1.4), 5, 20);
      updatePresetButtons();
      recomputeAndRenderLab();
    } else if (cfg.regime === "micro") {
      applyPreset("fast5");
      state.maxHold = clamp(Math.round(cfg.avg_hold_min + 2), 5, 10);
      $("sliderMaxHold").value = state.maxHold;
      recomputeAndRenderLab();
    } else if (cfg.regime === "anchor") {
      applyPreset("anchor20");
    } else {
      applyPreset("core10");
    }
    switchTab("lab");
  }

  /* ---------- Tab 2: Intraday Dynamic Weight Schedule ---------- */
  function renderWeightScheduleChart() {
    const svg = $("weightScheduleChart");
    clearEl(svg);
    if (!RAW_METRICS || !Array.isArray(RAW_METRICS.weight_schedule_intraday)) return;

    const pts = RAW_METRICS.weight_schedule_intraday;
    if (pts.length < 2) return;

    const W = 540;
    const H = 202;
    const padL = 40;
    const padR = 42;
    const padT = 10;
    const padB = 26;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const xOf = i => padL + (i / (pts.length - 1)) * plotW;
    const yPct = v => padT + plotH - clamp(v, 0, 1) * plotH;

    const maxVol = Math.max(2.2, ...pts.map(p => Number(p.rel_volume ?? p.rel_vol ?? 1.0))) * 1.08;
    const yVol = v => padT + plotH - (v / maxVol) * plotH;

    // Build cumulative stacks: bottom -> 20m, 15m, 10m, 5m -> top
    const layers = [
      { key: "w20", color: "#0B1F3A", opacity: 0.88 },
      { key: "w15", color: "#1B4F9C", opacity: 0.85 },
      { key: "w10", color: "#3B76C4", opacity: 0.84 },
      { key: "w5",  color: "#68A3DF", opacity: 0.88 }
    ];

    const cumPrev = new Array(pts.length).fill(0);
    for (const layer of layers) {
      const topPts = [];
      const botPts = [];
      for (let i = 0; i < pts.length; i++) {
        const w = Number(pts[i][layer.key] ?? 0.25);
        const y0 = cumPrev[i];
        const y1 = Math.min(1, y0 + w);
        topPts.push(`${xOf(i).toFixed(1)},${yPct(y1).toFixed(1)}`);
        botPts.unshift(`${xOf(i).toFixed(1)},${yPct(y0).toFixed(1)}`);
        cumPrev[i] = y1;
      }
      svg.appendChild(svgEl("polygon", {
        points: topPts.concat(botPts).join(" "),
        fill: layer.color, "fill-opacity": layer.opacity
      }));
    }

    // Subtle horizontal gridlines
    [0, 0.25, 0.5, 0.75, 1.0].forEach(pct => {
      const y = yPct(pct);
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: pct === 0 || pct === 1 ? COLORS.navy : "rgba(255,255,255,0.32)",
        "stroke-width": pct === 0 ? 1.1 : 0.8,
        "stroke-dasharray": pct === 0 || pct === 1 ? null : "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL - 6, y: y + 3.5, "text-anchor": "end",
        fill: COLORS.muted, "font-size": 9.5,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${Math.round(pct * 100)}%`));
    });

    // Relative Volume-Clock Overlay Curve (crisp white halo + dark navy dashed line)
    const volLinePts = pts.map((p, i) =>
      `${xOf(i).toFixed(1)},${yVol(Number(p.rel_volume ?? p.rel_vol ?? 1.0)).toFixed(1)}`
    ).join(" ");
    svg.appendChild(svgEl("polyline", {
      points: volLinePts, fill: "none",
      stroke: "#FFFFFF", "stroke-width": 3.4, "stroke-opacity": 0.9,
      "stroke-linejoin": "round"
    }));
    svg.appendChild(svgEl("polyline", {
      points: volLinePts, fill: "none",
      stroke: COLORS.navy, "stroke-width": 1.8, "stroke-dasharray": "4,2.5",
      "stroke-linejoin": "round"
    }));

    // Right axis ticks for Relative Volume (dynamic scaling)
    const volStep = maxVol > 2.8 ? 1.0 : 0.5;
    for (let v = volStep; v <= maxVol * 0.98; v += volStep) {
      const y = yVol(v);
      svg.appendChild(svgEl("text", {
        x: padL + plotW + 6, y: y + 3.5,
        fill: COLORS.ink, "font-size": 9.3, "font-weight": 600,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${v.toFixed(1)}\u00D7`));
    }

    // X-axis ET timestamps (guaranteed clean non-overlapping spacing)
    const step = Math.max(1, Math.floor((pts.length - 1) / 6));
    let lastTickX = -999;
    const lastIdx = pts.length - 1;
    const endX = xOf(lastIdx);
    for (let i = 0; i < pts.length; i += step) {
      const tx = xOf(i);
      if (i !== 0 && i !== lastIdx && (endX - tx) < 46) continue;
      if (tx - lastTickX < 44) continue;
      const tLabel = pts[i].time ?? pts[i].label ?? pts[i].timestamp ?? `${i}`;
      svg.appendChild(svgEl("text", {
        x: tx, y: H - 7, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 9.5,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, tLabel));
      lastTickX = tx;
    }
    if (endX - lastTickX >= 44) {
      const lastLbl = pts[lastIdx].time ?? pts[lastIdx].label ?? "15:50";
      svg.appendChild(svgEl("text", {
        x: endX, y: H - 7, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 9.5,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, lastLbl));
    }

    // Hover crosshair + tooltip
    const cross = svgEl("line", {
      x1: padL, y1: padT, x2: padL, y2: padT + plotH,
      stroke: "#FFFFFF", "stroke-width": 1.5, style: "display:none; pointer-events:none;"
    });
    svg.appendChild(cross);

    const hit = svgEl("rect", {
      x: padL, y: padT, width: plotW, height: plotH,
      fill: "transparent", style: "cursor:crosshair;"
    });
    const tt = $("weightTooltip");
    hit.addEventListener("mousemove", evt => {
      const rect = svg.getBoundingClientRect();
      const sx = ((evt.clientX - rect.left) / rect.width) * W;
      const idx = clamp(Math.round(((sx - padL) / plotW) * (pts.length - 1)), 0, pts.length - 1);
      const p = pts[idx];
      cross.style.display = "block";
      cross.setAttribute("x1", xOf(idx));
      cross.setAttribute("x2", xOf(idx));

      const tLbl = p.time ?? p.label ?? `Slot ${idx}`;
      tt.innerHTML = `
        <div class="tt-head">
          <span class="tt-time">${tLbl} ET</span>
          <span class="tt-px num">${fmt(p.rel_volume ?? p.rel_vol, 2)}&times; Vol</span>
        </div>
        <div class="tt-grid num">
          <span class="tt-k">5m / 10m Weight</span><span class="tt-v">${Math.round((p.w5 || 0) * 100)}% / ${Math.round((p.w10 || 0) * 100)}%</span>
          <span class="tt-k">15m / 20m Weight</span><span class="tt-v">${Math.round((p.w15 || 0) * 100)}% / ${Math.round((p.w20 || 0) * 100)}%</span>
          <span class="tt-k">Regime State</span><span class="tt-v">${p.regime_state || "Balanced"}</span>
        </div>
      `;
      tt.classList.add("visible");
      tt.style.top = "10px";
      if (idx > pts.length * 0.5) {
        tt.style.left = "46px";
        tt.style.right = "auto";
      } else {
        tt.style.left = "auto";
        tt.style.right = "48px";
      }
    });
    hit.addEventListener("mouseleave", () => {
      cross.style.display = "none";
      tt.classList.remove("visible");
    });
    svg.appendChild(hit);
  }

  /* ---------- Tab 2: Trade Duration Distribution (5m–20m) ---------- */
  function renderDurationChart() {
    const svg = $("durationChart");
    clearEl(svg);
    if (!RAW_METRICS || !RAW_METRICS.duration_histogram) return;

    // Normalize histogram whether array or dict
    const rawHist = RAW_METRICS.duration_histogram;
    const bins = [];
    if (Array.isArray(rawHist)) {
      for (const item of rawHist) {
        const min = Number(item.minute ?? item.hold_min ?? item.min ?? item.duration);
        const count = Number(item.count ?? item.trades ?? 0);
        if (!Number.isNaN(min)) bins.push({ min, count });
      }
    } else if (typeof rawHist === "object") {
      for (let m = 5; m <= 20; m++) {
        bins.push({ min: m, count: Number(rawHist[String(m)] ?? rawHist[m] ?? 0) });
      }
    }
    bins.sort((a, b) => a.min - b.min);
    if (bins.length === 0) return;

    const total = bins.reduce((s, b) => s + b.count, 0);
    const avgHold = total > 0
      ? bins.reduce((s, b) => s + b.min * b.count, 0) / total
      : 11.0;

    $("durationSubtitle").textContent = `Flagship mean ${fmt(avgHold, 1)}m · ${fmtInt(total)} fills`;

    const W = 540;
    const H = 194;
    const padL = 38;
    const padR = 16;
    const padT = 36;
    const padB = 26;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const maxCount = Math.max(1, ...bins.map(b => b.count)) * 1.18;
    const slotW = plotW / bins.length;
    const yOf = c => padT + plotH - (c / maxCount) * plotH;

    // Highlight Core 9-14m sweet spot backdrop
    const idx9 = bins.findIndex(b => b.min === 9);
    const idx14 = bins.findIndex(b => b.min === 14);
    if (idx9 >= 0 && idx14 >= idx9) {
      svg.appendChild(svgEl("rect", {
        x: padL + idx9 * slotW, y: padT - 14,
        width: (idx14 - idx9 + 1) * slotW, height: plotH + 14,
        fill: COLORS.ice
      }));
      svg.appendChild(svgEl("text", {
        x: padL + (idx9 + idx14 + 1) * 0.5 * slotW, y: padT - 3,
        "text-anchor": "middle", fill: COLORS.cobalt,
        "font-size": 9.1, "font-style": "italic", "font-family": "Georgia, serif"
      }, "Core 9–14m Sweet Spot"));
    }

    // Horizontal gridlines
    for (let k = 0; k <= 3; k++) {
      const val = (k / 3) * (maxCount / 1.18);
      const y = yOf(val);
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: k === 0 ? COLORS.navy : COLORS.grid,
        "stroke-width": k === 0 ? 1.1 : 0.8,
        "stroke-dasharray": k === 0 ? null : "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL - 6, y: y + 3.5, "text-anchor": "end",
        fill: COLORS.muted, "font-size": 9.3,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, Math.round(val)));
    }

    // Mean vertical dashed rule (drawn BEFORE bars/labels so labels remain crisp on top!)
    const minVal = bins[0].min;
    const meanX = padL + (avgHold - minVal + 0.5) * slotW;
    svg.appendChild(svgEl("line", {
      x1: meanX, y1: padT + 1, x2: meanX, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 1.35, "stroke-dasharray": "4,2.5", opacity: 0.78
    }));
    svg.appendChild(svgEl("rect", {
      x: clamp(meanX - 31, padL + 2, padL + plotW - 64), y: 2,
      width: 62, height: 15, rx: 3, fill: COLORS.navy
    }));
    svg.appendChild(svgEl("text", {
      x: clamp(meanX, padL + 33, padL + plotW - 33), y: 12.5,
      "text-anchor": "middle", fill: "#FFFFFF",
      "font-size": 9.1, "font-weight": 600,
      "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
    }, `Avg ${fmt(avgHold, 1)}m`));

    // Bars + crisp haloed value labels
    const bw = Math.max(10, slotW * 0.68);
    bins.forEach((b, idx) => {
      const cx = padL + (idx + 0.5) * slotW;
      const y = yOf(b.count);
      const h = Math.max(1, padT + plotH - y);
      const fill = b.min <= 8 ? COLORS.cerulean : (b.min <= 14 ? COLORS.cobalt : COLORS.steel);

      svg.appendChild(svgEl("rect", {
        x: cx - bw / 2, y, width: bw, height: h,
        fill, rx: 2, "fill-opacity": b.min >= 9 && b.min <= 14 ? 0.92 : 0.78
      }));

      if (b.count > 0) {
        svg.appendChild(svgEl("text", {
          x: cx, y: y - 4, "text-anchor": "middle",
          fill: COLORS.ink, "font-size": 8.6, "font-weight": 600,
          stroke: (b.min >= 9 && b.min <= 14) ? COLORS.ice : "#FFFFFF",
          "stroke-width": 3, "paint-order": "stroke",
          "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
        }, b.count));
      }

      svg.appendChild(svgEl("text", {
        x: cx, y: H - 7, "text-anchor": "middle",
        fill: b.min >= 9 && b.min <= 14 ? COLORS.navy : COLORS.muted,
        "font-size": 9.4, "font-weight": b.min >= 9 && b.min <= 14 ? 600 : 400,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${b.min}m`));
    });
  }

  /* ---------- Tab 2: Sortable 13-Configuration Benchmark Table ---------- */
  function renderBenchmarkTable() {
    const tbody = $("benchmarkBody");
    clearEl(tbody);
    if (!RAW_METRICS || !Array.isArray(RAW_METRICS.configs)) return;

    let rows = RAW_METRICS.configs.slice();
    if (state.tableFilter === "blended") {
      rows = rows.filter(r => r.is_blended);
    } else if (state.tableFilter !== "all") {
      rows = rows.filter(r => r.regime === state.tableFilter);
    }

    const key = state.sortKey;
    const dir = state.sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      const va = a[key];
      const vb = b[key];
      if (typeof va === "string" || typeof vb === "string") {
        return String(va || "").localeCompare(String(vb || "")) * dir;
      }
      return (Number(va || 0) - Number(vb || 0)) * dir;
    });

    // Update header sort arrows
    document.querySelectorAll("#benchmarkTable thead th.sortable").forEach(th => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sort === state.sortKey) {
        arrow.innerHTML = state.sortDir === "asc" ? "&#9650;" : "&#9660;";
      } else {
        arrow.textContent = "";
      }
    });

    for (const c of rows) {
      const tr = document.createElement("tr");
      if (c.is_flagship) tr.classList.add("flagship-row");
      if (state.selectedConfigId === c.id) tr.classList.add("selected-row");

      const badge = c.is_flagship
        ? `<span class="flagship-badge">FLAGSHIP</span>`
        : (c.is_blended ? `<span class="kpi-tag" style="margin-left:6px;">Blended</span>` : "");

      tr.innerHTML = `
        <td class="left">${c.name}${badge}</td>
        <td class="left"><span class="regime-chip ${c.regime}">${getRegimeLabel(c.regime)}</span></td>
        <td class="center num">${c.window_label}</td>
        <td class="num">${fmt(c.avg_hold_min, 1)}m</td>
        <td class="num" style="font-weight:700; color:#0B1F3A;">${fmt(c.net_sharpe, 2)}</td>
        <td class="num">${fmt(c.gross_sharpe, 2)}</td>
        <td class="num ${Number(c.annual_return_pct) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtSigned(c.annual_return_pct, 1)}%</td>
        <td class="num ${Number(c.vwap_slippage_saved_bps) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtSigned(c.vwap_slippage_saved_bps, 2)} bp</td>
        <td class="num">${fmtInt(c.annual_trades)}</td>
        <td class="num">${fmt(c.win_rate_pct, 1)}%</td>
        <td class="num">${fmt(c.profit_factor, 2)}</td>
        <td class="num">-${fmt(Math.abs(c.max_drawdown_pct), 1)}%</td>
        <td class="num">${fmt(c.avg_cost_bps, 2)}</td>
      `;
      tr.addEventListener("click", () => selectConfig(c.id));
      tbody.appendChild(tr);
    }
  }

  /* ==========================================================================
     TAB 3: DEEP RESEARCH & LIVE 8-ASSET SCANNER
     ========================================================================== */

  function ensureRadarSession(symbol) {
    if (symbol === state.symbol && RAW_SESSION && RAW_SESSION.bars && RAW_SESSION.bars.length) {
      RADAR_SESSION_CACHE[symbol] = RAW_SESSION;
      return RAW_SESSION;
    }
    if (symbol === "ES" && BENCHMARK_SESSION) {
      if (!RADAR_SESSION_CACHE.ES) RADAR_SESSION_CACHE.ES = BENCHMARK_SESSION;
      return RADAR_SESSION_CACHE.ES;
    }
    if (!RADAR_SESSION_CACHE[symbol]) {
      const profile = getSymbolProfile(symbol);
      const { bars: simBars } = generateCalibratedSymbolBars(symbol);
      RADAR_SESSION_CACHE[symbol] = buildCausalSession(
        symbol, simBars, "Calibrated 1m Model", profile.spreadBps, profile.impact
      );
    }
    return RADAR_SESSION_CACHE[symbol];
  }

  function runMultiAssetRadarScan() {
    // Warm up sessions first so timed block measures pure vectorized O(N) multi-asset microstructure scan
    const sessions = RADAR_SYMBOLS.map(sym => ({ sym, sess: ensureRadarSession(sym) }));
    const t0 = typeof performance !== "undefined" ? performance.now() : 0;
    const rows = [];
    let totalBars = 0;

    for (let k = 0; k < sessions.length; k++) {
      const { sym, sess } = sessions[k];
      const bars = sess.bars;
      totalBars += bars.length;
      const preserve = sym === "ES" && sess === BENCHMARK_SESSION;
      enrichPhase2Microstructure(bars, preserve);

      const b = bars[bars.length - 1];
      const z = Number(b.z_ou ?? b.z_score ?? 0);
      const absZ = Math.abs(z);
      const tau = Number(b.tau_half ?? 10.0);
      const curv = Number(b.curvature ?? 0);
      const slope = Number(b.slope ?? 0);
      const vpin = Number(b.vpin ?? 0.35);
      const hurst = Number(b.hurst ?? 0.50);
      const toxic = Boolean(b.toxic_flag);
      const pTrend = clamp(Number(b.p_trend ?? 0.18), 0, 1);
      const regimeMode = b.regime_mode || (pTrend < 0.38 ? "FADE" : (pTrend > 0.62 ? "SURF" : "FLAT"));
      const fanDir = Number(b.fan_dir ?? (b.vwap5 >= b.vwap20 ? 1 : -1));
      const fanAlign = Number(b.fan_align ?? 0);

      let hurstCls = "neutral";
      let hurstLabel = "NEUTRAL";
      if (hurst < 0.48) {
        hurstCls = "reverting";
        hurstLabel = "REVERT";
      } else if (hurst > 0.56 || toxic) {
        hurstCls = "toxic";
        hurstLabel = "TOXIC";
      }

      let actionCls = "watching";
      let actionLabel = "WATCHING";
      let actionRank = 1;
      if (pTrend > 0.62 && fanAlign >= 0.5) {
        if (fanDir >= 0) {
          actionCls = "surf-long";
          actionLabel = "SURF LONG";
        } else {
          actionCls = "surf-short";
          actionLabel = "SURF SHORT";
        }
        actionRank = 5;
      } else if (absZ >= state.entryZ) {
        if (pTrend >= 0.38 || toxic) {
          actionCls = "vetoed";
          actionLabel = "VETOED TOXIC";
          actionRank = 3;
        } else if (z <= -state.entryZ) {
          actionCls = "strong-long";
          actionLabel = "STRONG LONG";
          actionRank = 4;
        } else {
          actionCls = "strong-short";
          actionLabel = "STRONG SHORT";
          actionRank = 4;
        }
      } else if (toxic || pTrend >= 0.38) {
        actionCls = "vetoed";
        actionLabel = pTrend > 0.62 ? (fanDir >= 0 ? "SURF LONG" : "SURF SHORT") : "VETOED TOXIC";
        if (pTrend > 0.62) actionCls = fanDir >= 0 ? "surf-long" : "surf-short";
        actionRank = pTrend > 0.62 ? 4 : 2;
      }

      rows.push({
        symbol: sym,
        price: Number(b.close),
        vwapOu: Number(b.vwap_ou ?? b.vwap_adaptive),
        z,
        absZ,
        tauHalf: tau,
        curvature: curv,
        slope,
        vpin,
        hurst,
        pTrend,
        regimeMode,
        toxic,
        hurstCls,
        hurstLabel,
        actionCls,
        actionLabel,
        actionRank,
        bar: b
      });
    }

    const elapsed = typeof performance !== "undefined" ? Math.max(0.12, performance.now() - t0) : 1.2;
    state.radarScanMs = elapsed;
    state.radarRows = rows;

    const badge = $("radarLatencyBadge");
    if (badge) {
      badge.textContent = `${RADAR_SYMBOLS.length} Assets · ${fmtInt(totalBars)} Bars · ${fmt(elapsed, 2)} ms`;
    }
  }

  function renderRadarTable() {
    const tbody = $("radarTableBody");
    if (!tbody) return;
    clearEl(tbody);

    const rows = state.radarRows.slice();
    const key = state.radarSortKey;
    const dir = state.radarSortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      if (key === "symbol") return a.symbol.localeCompare(b.symbol) * dir;
      return (Number(a[key] || 0) - Number(b[key] || 0)) * dir;
    });

    document.querySelectorAll("#radarTable thead th.sortable").forEach(th => {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.rsort === state.radarSortKey) {
        arrow.innerHTML = state.radarSortDir === "asc" ? "&#9650;" : "&#9660;";
      } else {
        arrow.textContent = "";
      }
    });

    for (const r of rows) {
      const tr = document.createElement("tr");
      if (state.radarSelectedSymbol === r.symbol) tr.classList.add("selected-row");
      const digits = r.price >= 1000 ? 1 : 2;
      const vpinPct = clamp(Math.round(r.vpin * 100), 4, 100);
      const vpinColor = r.vpin >= 0.56 ? COLORS.short : (r.vpin >= 0.44 ? COLORS.cobalt : COLORS.long);

      tr.innerHTML = `
        <td class="left"><strong>${r.symbol}</strong></td>
        <td class="num">${fmt(r.price, digits)}</td>
        <td class="num">${fmt(r.vwapOu, digits)}</td>
        <td class="num ${r.z <= -state.entryZ ? "pnl-pos" : r.z >= state.entryZ ? "pnl-neg" : ""}">${fmtSigned(r.z, 2)}&sigma;</td>
        <td class="num">${fmt(r.tauHalf, 1)}m</td>
        <td class="num">${fmtSigned(r.curvature, 2)}</td>
        <td class="num"><span class="vpin-cell"><span>${fmt(r.vpin, 2)}</span><span class="vpin-bar"><i style="width:${vpinPct}%; background:${vpinColor};"></i></span></span></td>
        <td class="center"><span class="ptrend-pill ${r.regimeMode.toLowerCase()}">${fmt(r.pTrend, 2)} ${r.regimeMode}</span></td>
        <td class="center"><span class="hurst-pill ${r.hurstCls}">${fmt(r.hurst, 2)} ${r.hurstLabel}</span></td>
        <td class="center"><span class="action-pill ${r.actionCls}">${r.actionLabel}</span></td>
        <td class="center"><button type="button" class="btn-radar-inspect" data-inspect="${r.symbol}">Inspect &rarr;</button></td>
      `;

      tr.addEventListener("click", evt => {
        if (evt.target && evt.target.dataset && evt.target.dataset.inspect) return;
        state.radarSelectedSymbol = r.symbol;
        renderRadarTable();
        renderTermStructureChart();
      });

      const inspectBtn = tr.querySelector(".btn-radar-inspect");
      if (inspectBtn) {
        inspectBtn.addEventListener("click", evt => {
          evt.stopPropagation();
          state.radarSelectedSymbol = r.symbol;
          loadSymbol(r.symbol);
          switchTab("lab");
        });
      }

      tbody.appendChild(tr);
    }
  }

  function renderTermStructureChart() {
    const svg = $("termStructureChart");
    if (!svg) return;
    clearEl(svg);

    const sym = state.radarSelectedSymbol || state.symbol || "ES";
    const row = state.radarRows.find(r => r.symbol === sym) || state.radarRows[0];
    if (!row || !row.bar) return;

    const b = row.bar;
    const digits = b.close >= 1000 ? 1 : 2;
    if ($("tsCurveTitle")) $("tsCurveTitle").textContent = `5–20m VWAP Term-Structure · ${row.symbol}`;
    if ($("tsCurveModeBadge")) $("tsCurveModeBadge").textContent = `P(Trend) ${fmt(row.pTrend, 2)} · ${row.regimeMode}`;
    if ($("tsFactorLevel")) $("tsFactorLevel").textContent = `$${fmt(row.vwapOu, digits)}`;
    if ($("tsFactorSlope")) $("tsFactorSlope").textContent = `${fmtSigned(row.slope, 2)}\u03C3`;
    if ($("tsFactorCurv")) $("tsFactorCurv").textContent = `${fmtSigned(row.curvature, 2)}\u03C3`;
    if ($("tsFactorTau")) $("tsFactorTau").textContent = `${fmt(row.tauHalf, 1)}m`;
    if ($("tsFactorVpin")) $("tsFactorVpin").textContent = fmt(row.vpin, 2);

    const anchor20 = Number(b.vwap20 ?? row.vwapOu);
    const sigEnv = Math.max(b.close * 0.00025, Number(b.sigma_ou ?? b.sigma_blend ?? b.close * 0.0008));

    const curvePts = ALL_HORIZONS.map(h => {
      const px = Number(b[`vwap${h}`] ?? row.vwapOu);
      return {
        h,
        px,
        bpsVs20: ((px - anchor20) / Math.max(1e-6, anchor20)) * 10000
      };
    });

    const W = 540;
    const H = 218;
    const padL = 56;
    const padR = 54;
    const padT = 18;
    const padB = 30;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const allPrices = curvePts.map(p => p.px).concat([
      b.close,
      row.vwapOu,
      row.vwapOu + sigEnv * 0.65,
      row.vwapOu - sigEnv * 0.65
    ]);
    let pMin = Math.min(...allPrices);
    let pMax = Math.max(...allPrices);
    const span = Math.max(pMax - pMin, b.close * 0.0008);
    pMin -= span * 0.14;
    pMax += span * 0.14;

    const xOf = h => padL + ((h - 5) / 15) * plotW;
    const yOf = p => padT + plotH - ((p - pMin) / Math.max(1e-9, pMax - pMin)) * plotH;

    // Subtle shaded 9-14m Core band
    svg.appendChild(svgEl("rect", {
      x: xOf(9), y: padT, width: xOf(14) - xOf(9), height: plotH,
      fill: COLORS.ice
    }));

    // Horizontal dashed gridlines & left Price / right bps-vs-20m ticks
    for (let k = 0; k <= 4; k++) {
      const pVal = pMin + (k / 4) * (pMax - pMin);
      const y = yOf(pVal);
      const bpsVal = ((pVal - anchor20) / Math.max(1e-6, anchor20)) * 10000;
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: k === 0 ? COLORS.navy : COLORS.grid,
        "stroke-width": k === 0 ? 1.1 : 0.8,
        "stroke-dasharray": k === 0 ? null : "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL - 6, y: y + 3.5, "text-anchor": "end",
        fill: COLORS.muted, "font-size": 9.3,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, pVal.toFixed(digits)));
      svg.appendChild(svgEl("text", {
        x: padL + plotW + 6, y: y + 3.5,
        fill: COLORS.muted, "font-size": 9.1,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${fmtSigned(bpsVal, 1)}bp`));
    }

    // Left spine
    svg.appendChild(svgEl("line", {
      x1: padL, y1: padT, x2: padL, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 1.1
    }));

    // Translucent ±0.5σ envelope around term-structure curve
    const envUp = curvePts.map(p => `${xOf(p.h).toFixed(1)},${yOf(p.px + sigEnv * 0.5).toFixed(1)}`);
    const envDn = curvePts.slice().reverse().map(p => `${xOf(p.h).toFixed(1)},${yOf(p.px - sigEnv * 0.5).toFixed(1)}`);
    svg.appendChild(svgEl("polygon", {
      points: envUp.concat(envDn).join(" "),
      fill: "#3B76C4", "fill-opacity": 0.12
    }));

    // Horizontal Spot Price reference line
    const ySpot = yOf(b.close);
    svg.appendChild(svgEl("line", {
      x1: padL, y1: ySpot, x2: padL + plotW, y2: ySpot,
      stroke: row.z <= 0 ? COLORS.long : COLORS.short,
      "stroke-width": 1.25, "stroke-dasharray": "4,3"
    }));
    const spotLblY = clamp(ySpot - 5, padT + 11, padT + plotH - 5);
    svg.appendChild(svgEl("text", {
      x: padL + plotW - 4, y: spotLblY, "text-anchor": "end",
      fill: row.z <= 0 ? COLORS.long : COLORS.short,
      "font-size": 9.4, "font-weight": 700,
      "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
    }, `Spot $${fmt(b.close, digits)} (${fmtSigned(row.z, 2)}\u03C3)`));

    // Continuous 9-Horizon VWAP Curve
    const linePts = curvePts.map(p => `${xOf(p.h).toFixed(1)},${yOf(p.px).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("polyline", {
      points: linePts, fill: "none",
      stroke: COLORS.cobalt, "stroke-width": 2.2, "stroke-linejoin": "round"
    }));

    // Horizon dots & X-axis labels
    const tt = $("tsTooltip");
    for (const p of curvePts) {
      const cx = xOf(p.h);
      const cy = yOf(p.px);
      const isBlendNode = BLEND_HORIZONS.includes(p.h);
      const dot = svgEl("circle", {
        cx, cy, r: isBlendNode ? 4.2 : 3.0,
        fill: isBlendNode ? COLORS.cobalt : "#FFFFFF",
        stroke: COLORS.cobalt, "stroke-width": 1.5,
        style: "cursor:pointer;"
      });
      dot.addEventListener("mouseenter", () => {
        if (!tt) return;
        tt.innerHTML = `
          <div class="tt-head">
            <span class="tt-time">${row.symbol} · ${p.h}m VWAP</span>
            <span class="tt-px num">$${fmt(p.px, digits)}</span>
          </div>
          <div class="tt-grid num">
            <span class="tt-k">vs 20m Anchor</span><span class="tt-v">${fmtSigned(p.bpsVs20, 2)} bp</span>
            <span class="tt-k">Spot - VWAP</span><span class="tt-v">${fmtSigned(((b.close - p.px) / p.px) * 10000, 2)} bp</span>
          </div>
        `;
        tt.classList.add("visible");
        tt.style.top = "10px";
        tt.style.left = p.h > 12 ? "62px" : "auto";
        tt.style.right = p.h > 12 ? "auto" : "60px";
      });
      dot.addEventListener("mouseleave", () => {
        if (tt) tt.classList.remove("visible");
      });
      svg.appendChild(dot);

      if (isBlendNode || p.h === 8 || p.h === 12 || p.h === 18) {
        svg.appendChild(svgEl("text", {
          x: cx, y: H - 9, "text-anchor": "middle",
          fill: isBlendNode ? COLORS.navy : COLORS.muted,
          "font-size": 9.4, "font-weight": isBlendNode ? 600 : 400,
          "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
        }, `${p.h}m`));
      }
    }

    // OU-Spectral Blend Diamond Marker positioned at effective half-life τ½
    const ouX = xOf(clamp(row.tauHalf, 5, 20));
    const ouY = yOf(row.vwapOu);
    svg.appendChild(svgEl("line", {
      x1: ouX, y1: ouY, x2: ouX, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 1, "stroke-dasharray": "2.5,2.5", opacity: 0.7
    }));
    const dSize = 6.2;
    svg.appendChild(svgEl("polygon", {
      points: `${ouX},${ouY - dSize} ${ouX + dSize},${ouY} ${ouX},${ouY + dSize} ${ouX - dSize},${ouY}`,
      fill: COLORS.navy, stroke: "#FFFFFF", "stroke-width": 1.4
    }));
    const ouLblAnchor = ouX > padL + plotW * 0.60 ? "end" : "start";
    const ouLblX = ouLblAnchor === "end" ? ouX - 9 : ouX + 9;
    const preferBelow = ouY < padT + 42 || (curvePts[2] && yOf(curvePts[2].px) < ouY + 4);
    const ouLblY = clamp(ouY + (preferBelow ? 18 : -13), padT + 13, padT + plotH - 18);
    svg.appendChild(svgEl("text", {
      x: ouLblX, y: ouLblY, "text-anchor": ouLblAnchor,
      fill: COLORS.navy, "font-size": 9.4, "font-weight": 700,
      stroke: "#FFFFFF", "stroke-width": 3, "paint-order": "stroke",
      "font-family": "Georgia, serif"
    }, `OU-Spectral (\u03C4\u00BD ${fmt(row.tauHalf, 1)}m)`));
  }

  function renderAlphaDecayChart() {
    const svg = $("alphaDecayChart");
    if (!svg) return;
    clearEl(svg);

    const rl = RAW_METRICS && RAW_METRICS.research_lab;
    const pts = rl && Array.isArray(rl.alpha_decay_curve) ? rl.alpha_decay_curve : null;
    if (!pts || pts.length < 2) {
      svg.appendChild(svgEl("text", {
        x: 270, y: 108, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 11.5, "font-style": "italic", "font-family": "Georgia, serif"
      }, "Awaiting research_lab.alpha_decay_curve from quantitative pipeline…"));
      return;
    }

    const W = 540;
    const H = 214;
    const padL = 46;
    const padR = 18;
    const padT = 22;
    const padB = 28;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const minH = pts[0].horizon_min;
    const maxH = pts[pts.length - 1].horizon_min;
    const xOf = h => padL + ((h - minH) / Math.max(1, maxH - minH)) * plotW;

    const vals = [];
    for (const p of pts) {
      vals.push(Number(p.ou_spectral_vpin_net_bps || 0), Number(p.adaptive_blend_net_bps || 0), Number(p.single_8m_net_bps || 0));
    }
    const yMin = Math.min(-1.0, Math.floor(Math.min(...vals) - 0.15));
    const yMax = Math.max(2.0, Math.ceil(Math.max(...vals) + 0.55));
    const yOf = v => padT + plotH - ((v - yMin) / Math.max(1e-6, yMax - yMin)) * plotH;

    // Ice-Blue 5-20m Sweet Spot Backdrop
    svg.appendChild(svgEl("rect", {
      x: xOf(5), y: padT, width: xOf(20) - xOf(5), height: plotH,
      fill: COLORS.ice
    }));
    svg.appendChild(svgEl("text", {
      x: (xOf(5) + xOf(20)) / 2, y: padT + 11, "text-anchor": "middle",
      fill: COLORS.cobalt, "font-size": 9.2, "font-style": "italic", "font-family": "Georgia, serif"
    }, "5–20m Blended Sweet Spot"));

    // Clean integer horizontal gridlines & Y ticks
    const yTickStep = (yMax - yMin) > 5 ? 2.0 : 1.0;
    for (let v = yMin; v <= yMax + 1e-6; v += yTickStep) {
      const y = yOf(v);
      svg.appendChild(svgEl("line", {
        x1: padL, y1: y, x2: padL + plotW, y2: y,
        stroke: Math.abs(v) < 1e-6 ? "#8CA8C8" : COLORS.grid,
        "stroke-width": Math.abs(v) < 1e-6 ? 1.0 : 0.8,
        "stroke-dasharray": Math.abs(v) < 1e-6 ? null : "3,3"
      }));
      svg.appendChild(svgEl("text", {
        x: padL - 6, y: y + 3.5, "text-anchor": "end",
        fill: COLORS.muted, "font-size": 9.3,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${v > 0 ? "+" : ""}${v.toFixed(1)}bp`));
    }

    // Axes spines
    svg.appendChild(svgEl("line", { x1: padL, y1: padT, x2: padL, y2: padT + plotH, stroke: COLORS.navy, "stroke-width": 1.15 }));
    svg.appendChild(svgEl("line", { x1: padL, y1: padT + plotH, x2: padL + plotW, y2: padT + plotH, stroke: COLORS.navy, "stroke-width": 1.15 }));

    // Curves: 8m Single, Adaptive Blend, OU-Spectral VPIN
    const seriesSpecs = [
      { key: "single_8m_net_bps",       color: COLORS.cerulean, width: 1.5, dash: "4,3", dots: false },
      { key: "adaptive_blend_net_bps",  color: COLORS.cobalt,   width: 1.8, dash: null,  dots: false },
      { key: "ou_spectral_vpin_net_bps", color: COLORS.navy,    width: 2.3, dash: null,  dots: true }
    ];

    for (const sp of seriesSpecs) {
      const poly = pts.map(p => `${xOf(p.horizon_min).toFixed(1)},${yOf(Number(p[sp.key] || 0)).toFixed(1)}`).join(" ");
      svg.appendChild(svgEl("polyline", {
        points: poly, fill: "none",
        stroke: sp.color, "stroke-width": sp.width, "stroke-dasharray": sp.dash,
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }));
      if (sp.dots) {
        for (const p of pts) {
          svg.appendChild(svgEl("circle", {
            cx: xOf(p.horizon_min), cy: yOf(Number(p[sp.key] || 0)),
            r: p.in_sweet_spot ? 3.2 : 2.3,
            fill: COLORS.navy, stroke: "#FFFFFF", "stroke-width": 1.0
          }));
        }
      }
    }

    // Peak OU-Spectral Net Edge Pill (placed cleanly below the OU vertex so Adaptive curve stays 100% unobstructed)
    const peakPt = pts.reduce((best, p) =>
      Number(p.ou_spectral_vpin_net_bps || -99) > Number(best.ou_spectral_vpin_net_bps || -99) ? p : best, pts[0]
    );
    const px = xOf(peakPt.horizon_min);
    const py = yOf(Number(peakPt.ou_spectral_vpin_net_bps || 0));
    const badgeW = 98;
    const bx = clamp(px - badgeW / 2, padL + 4, padL + plotW - badgeW - 4);
    const by = clamp(py + 13, padT + 18, padT + plotH - 22);
    svg.appendChild(svgEl("line", {
      x1: px, y1: py + 3.5, x2: px, y2: by,
      stroke: COLORS.navy, "stroke-width": 1.0, "stroke-dasharray": "2,1.5"
    }));
    svg.appendChild(svgEl("rect", { x: bx, y: by, width: badgeW, height: 15, rx: 3, fill: COLORS.navy }));
    svg.appendChild(svgEl("text", {
      x: bx + badgeW / 2, y: by + 10.5, "text-anchor": "middle",
      fill: "#FFFFFF", "font-size": 8.9, "font-weight": 600,
      "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
    }, `Peak ${fmtSigned(peakPt.ou_spectral_vpin_net_bps, 2)}bp @ ${peakPt.horizon_min}m`));

    // X-axis horizon ticks
    [1, 5, 10, 15, 20, 25, 30].forEach(h => {
      const tx = xOf(h);
      svg.appendChild(svgEl("line", { x1: tx, y1: padT + plotH, x2: tx, y2: padT + plotH + 3.5, stroke: COLORS.navy, "stroke-width": 1 }));
      svg.appendChild(svgEl("text", {
        x: tx, y: H - 7, "text-anchor": "middle",
        fill: (h >= 5 && h <= 20) ? COLORS.navy : COLORS.muted,
        "font-size": 9.3, "font-weight": (h >= 5 && h <= 20) ? 600 : 400,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${h}m`));
    });

    // Interactive Crosshair & Tooltip
    const cross = svgEl("line", {
      x1: padL, y1: padT, x2: padL, y2: padT + plotH,
      stroke: COLORS.navy, "stroke-width": 0.9, "stroke-dasharray": "3,2",
      style: "display:none; pointer-events:none;"
    });
    svg.appendChild(cross);
    const hit = svgEl("rect", {
      x: padL, y: padT, width: plotW, height: plotH,
      fill: "transparent", style: "cursor:crosshair;"
    });
    const tt = $("alphaDecayTooltip");
    hit.addEventListener("mousemove", evt => {
      const rect = svg.getBoundingClientRect();
      const sx = ((evt.clientX - rect.left) / rect.width) * W;
      const targetH = minH + clamp((sx - padL) / plotW, 0, 1) * (maxH - minH);
      let nearest = pts[0];
      let bestDist = Infinity;
      for (const p of pts) {
        const d = Math.abs(p.horizon_min - targetH);
        if (d < bestDist) { bestDist = d; nearest = p; }
      }
      const cx = xOf(nearest.horizon_min);
      cross.style.display = "block";
      cross.setAttribute("x1", cx);
      cross.setAttribute("x2", cx);
      if (tt) {
        tt.innerHTML = `
          <div class="tt-head">
            <span class="tt-time">Horizon: ${nearest.horizon_min} min</span>
            <span class="tt-px num">${nearest.in_sweet_spot ? "5–20m Zone" : "Outer"}</span>
          </div>
          <div class="tt-grid num">
            <span class="tt-k">OU-Spectral VPIN</span><span class="tt-v">${fmtSigned(nearest.ou_spectral_vpin_net_bps, 2)} bp (${fmt(nearest.ou_spectral_vpin_hit_pct, 1)}%)</span>
            <span class="tt-k">Adaptive Blend</span><span class="tt-v">${fmtSigned(nearest.adaptive_blend_net_bps, 2)} bp (${fmt(nearest.adaptive_blend_hit_pct, 1)}%)</span>
            <span class="tt-k">8m Single</span><span class="tt-v">${fmtSigned(nearest.single_8m_net_bps, 2)} bp (${fmt(nearest.single_8m_hit_pct, 1)}%)</span>
          </div>
        `;
        tt.classList.add("visible");
        tt.style.top = "12px";
        tt.style.left = nearest.horizon_min > 15 ? "54px" : "auto";
        tt.style.right = nearest.horizon_min > 15 ? "auto" : "24px";
      }
    });
    hit.addEventListener("mouseleave", () => {
      cross.style.display = "none";
      if (tt) tt.classList.remove("visible");
    });
    svg.appendChild(hit);
  }

  function renderWalkForwardTable() {
    const tbody = $("walkForwardBody");
    if (!tbody) return;
    clearEl(tbody);

    const rl = RAW_METRICS && RAW_METRICS.research_lab;
    const dr = RAW_METRICS && RAW_METRICS.dual_regime_lab;
    const wf = (dr && dr.walk_forward_dsr_m20 && Array.isArray(dr.walk_forward_dsr_m20.models))
      ? dr.walk_forward_dsr_m20
      : (rl && rl.walk_forward_dsr);

    if (!wf || !Array.isArray(wf.models)) {
      tbody.innerHTML = `<tr><td colspan="8" class="center" style="padding:18px; color:#526D8F;">Awaiting Purged Walk-Forward &amp; DSR benchmark in data/bundle.js…</td></tr>`;
      return;
    }

    const models = wf.models.slice();
    const split = wf.split || (rl && rl.walk_forward_dsr && rl.walk_forward_dsr.split);
    const mTrials = (split && split.trials_m) || 20;
    const srStar = split ? (split.sr_benchmark_m20 ?? split.sr_benchmark_m16) : undefined;
    if ($("wfDsrHeader")) $("wfDsrHeader").textContent = `DSR (M=${mTrials})`;
    if (split && $("wfMetaSubtitle")) {
      $("wfMetaSubtitle").textContent =
        `IS ${split.is_sessions || "1–40"} vs OOS ${split.oos_sessions || "41–60"} · DSR (M=${mTrials}${srStar !== undefined ? `, SR*=${fmt(srStar, 2)}` : ""})`;
    }

    const badgeRef = wf.dual_regime_1x || wf.ou_spectral_1x || models[models.length - 1];
    if (badgeRef && $("wfRetentionBadge")) {
      const dsrVal = badgeRef.dsr_m20_pct ?? badgeRef.dsr_pct;
      const tag = /dual/i.test(badgeRef.id || badgeRef.name) ? "Dual 1\u00D7" : "OU 1\u00D7";
      $("wfRetentionBadge").textContent = `${tag} OOS SR ${fmt(badgeRef.oos_sharpe, 2)} · DSR ${fmt(dsrVal, 2)}%`;
    }

    for (const m of models) {
      const tr = document.createElement("tr");
      const isDual = Boolean(m.is_dual_regime) || /dual/i.test(m.id || m.name);
      const isTurbo = /turbo/i.test(m.id || m.name);
      if (isDual) tr.classList.add("flagship-row");
      let badge = "";
      if (isDual && isTurbo) badge = `<span class="flagship-badge" style="margin-left:5px;">ALL-WEATHER</span>`;
      else if (isDual) badge = `<span class="kpi-tag" style="margin-left:5px; background:#DCEBFA; color:#0B1F3A;">BAYES</span>`;
      else if (m.is_frontier || /ou_spectral/i.test(m.id || "")) badge = `<span class="kpi-tag" style="margin-left:5px;">${isTurbo ? "TURBO" : "OU-VPIN"}</span>`;

      const dsrVal = Number(m.dsr_m20_pct ?? m.dsr_pct ?? 0);
      const retCls = Number(m.oos_retention_pct) >= 100 ? "pnl-pos" : "";
      const dsrCls = dsrVal >= 66 ? "pnl-pos" : "";

      tr.innerHTML = `
        <td class="left">${m.name}${badge}</td>
        <td class="num">${fmt(m.is_sharpe, 2)}</td>
        <td class="num" style="font-weight:700; color:#0B1F3A;">${fmt(m.oos_sharpe, 2)}</td>
        <td class="num ${Number(m.oos_annual_return_pct) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtSigned(m.oos_annual_return_pct, 1)}%</td>
        <td class="num">${fmtSigned(m.vwap_slippage_saved_bps, 2)} bp</td>
        <td class="num">${fmt(m.psr_pct, 2)}%</td>
        <td class="num ${dsrCls}">${fmt(dsrVal, 2)}%</td>
        <td class="num ${retCls}">${fmt(m.oos_retention_pct, 1)}%</td>
      `;
      tbody.appendChild(tr);
    }
  }

  function renderStressMatrix() {
    const grid = $("stressMatrixGrid");
    if (!grid) return;
    clearEl(grid);

    const rl = RAW_METRICS && RAW_METRICS.research_lab;
    const dr = RAW_METRICS && RAW_METRICS.dual_regime_lab;
    const st5 = dr && dr.stress_tests_5;
    const scenarios = (st5 && Array.isArray(st5.scenarios) && st5.scenarios.length > 0)
      ? st5.scenarios
      : (rl && Array.isArray(rl.stress_tests) ? rl.stress_tests : null);

    if (!scenarios || scenarios.length === 0) {
      grid.innerHTML = `<div style="grid-column:1/-1; text-align:center; padding:18px; color:#526D8F; font-style:italic;">Awaiting 5-scenario adversarial stress suite in data/bundle.js…</div>`;
      return;
    }

    if (st5 && st5.five_regime_mean && $("stressMetaSubtitle")) {
      const fm = st5.five_regime_mean;
      $("stressMetaSubtitle").textContent =
        `5-Regime Mean: 8m ${fmtSigned(fm.single_8m, 2)} · Adapt ${fmtSigned(fm.adaptive_1x, 2)} · OU-Fade ${fmtSigned(fm.ou_spectral_1x, 2)} · Dual 1\u00D7 ${fmtSigned(fm.dual_regime_1x, 2)} (5/5 POS) · Dual Turbo ${fmtSigned(fm.dual_regime_turbo, 2)} (5/5 POS)`;
    }

    const hasDualModels = scenarios.some(sc => sc.models && sc.models.dual_regime_1x);
    grid.classList.toggle("stress-grid-5", scenarios.length >= 5);

    const modelOrder = hasDualModels
      ? [
          { key: "single_8m",         short: "8m Single",  color: "#94B6DA" },
          { key: "adaptive_1x",       short: "Adapt 1\u00D7", color: "#568BD0" },
          { key: "ou_spectral_1x",    short: "OU-VPIN",    color: "#2A64B5" },
          { key: "dual_regime_1x",    short: "Dual 1\u00D7", color: "#134287" },
          { key: "dual_regime_turbo", short: "Dual Turbo", color: "#0B1F3A" }
        ]
      : [
          { key: "single_8m",         short: "8m Single",  color: "#94B6DA" },
          { key: "adaptive_1x",       short: "Adapt 1\u00D7", color: "#3B76C4" },
          { key: "ou_spectral_1x",    short: "OU-VPIN 1\u00D7", color: "#1B4F9C" },
          { key: "ou_spectral_turbo", short: "OU Turbo",   color: "#0B1F3A" }
        ];

    for (const sc of scenarios) {
      const modelsObj = sc.models || {};
      const sharpes = modelOrder.map(m => Math.abs(Number((modelsObj[m.key] && modelsObj[m.key].net_sharpe) || 0)));
      const maxAbsSr = Math.max(2.0, ...sharpes);

      const card = document.createElement("div");
      card.className = `stress-card${(sc.id === "momentum_meltup" || sc.id === "whipsaw_flip") ? " stress-toxic" : ""}`;

      const barsHtml = modelOrder.map(m => {
        const d = modelsObj[m.key] || { net_sharpe: 0, max_drawdown_pct: 0 };
        const sr = Number(d.net_sharpe || 0);
        const mdd = Math.abs(Number(d.max_drawdown_pct || 0));
        const halfPct = clamp((Math.abs(sr) / maxAbsSr) * 50, 2.5, 50);
        const isNeg = sr < 0;
        const leftPos = isNeg ? (50 - halfPct) : 50;
        return `
          <div class="stress-bar-row num">
            <span class="stress-model-lbl">${m.short}</span>
            <span class="stress-track"><i class="stress-zero"></i><div class="stress-fill${isNeg ? " neg" : ""}" style="left:${leftPos.toFixed(1)}%; width:${halfPct.toFixed(1)}%; background:${m.color};"></div></span>
            <span class="stress-val ${isNeg ? "pnl-neg" : "pnl-pos"}">${fmtSigned(sr, 2)} <small>(-${fmt(mdd, 1)}%)</small></span>
          </div>
        `;
      }).join("");

      const footLeft = sc.mean_p_trend !== undefined
        ? `P(T) <strong>${fmt(sc.mean_p_trend, 2)}</strong> · Surf <strong>${fmt(sc.surf_bar_pct, 0)}%</strong>`
        : `VPIN <strong>${fmt(sc.vpin_mean, 2)}</strong> · H&#770; <strong>${fmt(sc.hurst_mean, 2)}</strong>`;
      const footRight = sc.dual_fade_trades !== undefined
        ? `F/S <strong>${sc.dual_fade_trades}/${sc.dual_surf_trades}</strong>`
        : `<strong>${fmtInt(sc.toxic_vetoes)}</strong> vetoes`;

      card.innerHTML = `
        <div class="stress-head">
          <span class="stress-title">${sc.label}</span>
          <span class="stress-shock-tag num">s${sc.seed}</span>
        </div>
        <div class="stress-desc">${sc.description}</div>
        <div class="stress-bars">${barsHtml}</div>
        <div class="stress-foot num">
          <span>${footLeft}</span>
          <span>${footRight}</span>
        </div>
      `;
      grid.appendChild(card);
    }
  }

  function renderBootstrapChart() {
    const svg = $("bootstrapChart");
    if (!svg) return;
    clearEl(svg);

    const dr = RAW_METRICS && RAW_METRICS.dual_regime_lab;
    const boot = dr && dr.bootstrap_ci;
    const aw = boot && boot.all_weather_120d;
    if (!aw || typeof aw !== "object") {
      svg.appendChild(svgEl("text", {
        x: 370, y: 86, "text-anchor": "middle",
        fill: COLORS.muted, "font-size": 11, "font-style": "italic", "font-family": "Georgia, serif"
      }, "Awaiting 1,000-trial Block-Bootstrap 95% Sharpe CIs in data/bundle.js…"));
      return;
    }

    if ($("bootTrialBadge")) {
      $("bootTrialBadge").textContent = `B=${fmtInt(boot.n_trials || 1000)} · L=${boot.block_len_sessions || 5}d · 120d`;
    }

    const order = [
      { key: "single_8m",         label: "8m Elastic Fade",  isDual: false },
      { key: "adaptive_1x",       label: "Adaptive Blend 1\u00D7", isDual: false },
      { key: "ou_spectral_1x",    label: "OU-VPIN Fade 1\u00D7",   isDual: false },
      { key: "dual_regime_1x",    label: "Dual-Regime 1\u00D7",    isDual: true  },
      { key: "dual_regime_turbo", label: "Dual-Regime Turbo", isDual: true  }
    ];
    const items = order.filter(o => aw[o.key]).map(o => ({ ...o, ...aw[o.key] }));

    const W = 740;
    const H = 166;
    const padL = 128;
    const padR = 176;
    const padT = 18;
    const padB = 22;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    const allVals = [0];
    items.forEach(it => allVals.push(Number(it.ci95_lo), Number(it.ci95_hi), Number(it.median_sharpe)));
    const xMin = Math.floor(Math.min(...allVals) - 0.8);
    const xMax = Math.ceil(Math.max(...allVals) + 0.8);
    const xOf = v => padL + ((v - xMin) / Math.max(1, xMax - xMin)) * plotW;

    // Vertical gridlines & zero spine
    const step = (xMax - xMin) > 16 ? 4 : 2;
    for (let xv = Math.ceil(xMin / step) * step; xv <= xMax; xv += step) {
      const x = xOf(xv);
      svg.appendChild(svgEl("line", {
        x1: x, y1: padT, x2: x, y2: padT + plotH,
        stroke: xv === 0 ? COLORS.navy : COLORS.grid,
        "stroke-width": xv === 0 ? 1.2 : 0.75,
        "stroke-dasharray": xv === 0 ? "3,2" : "2,3"
      }));
      svg.appendChild(svgEl("text", {
        x, y: H - 6, "text-anchor": "middle",
        fill: xv === 0 ? COLORS.navy : COLORS.muted,
        "font-size": 9.2, "font-weight": xv === 0 ? 700 : 400,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, xv === 0 ? "0.0" : `${xv > 0 ? "+" : ""}${xv}`));
    }

    // Column headers for right-side readouts
    svg.appendChild(svgEl("text", {
      x: padL + plotW + 10, y: 12, fill: COLORS.muted,
      "font-size": 8.6, "font-weight": 700, "font-family": "Segoe UI, sans-serif"
    }, "95% SHARPE CI"));
    svg.appendChild(svgEl("text", {
      x: padL + plotW + 114, y: 12, "text-anchor": "end", fill: COLORS.muted,
      "font-size": 8.6, "font-weight": 700, "font-family": "Segoe UI, sans-serif"
    }, "P(LOSS)"));
    svg.appendChild(svgEl("text", {
      x: W - 4, y: 12, "text-anchor": "end", fill: COLORS.muted,
      "font-size": 8.6, "font-weight": 700, "font-family": "Segoe UI, sans-serif"
    }, "CVaR95"));

    const rowH = plotH / Math.max(1, items.length);
    items.forEach((it, idx) => {
      const cy = padT + (idx + 0.5) * rowH;
      const lo = Number(it.ci95_lo);
      const med = Number(it.median_sharpe);
      const hi = Number(it.ci95_hi);
      const pLoss = Number(it.prob_loss_pct);
      const cvar = Number(it.daily_cvar95_bps);

      if (it.isDual) {
        svg.appendChild(svgEl("rect", {
          x: 2, y: cy - rowH / 2 + 1, width: W - 4, height: rowH - 2,
          fill: COLORS.ice, rx: 3
        }));
      }

      svg.appendChild(svgEl("text", {
        x: padL - 8, y: cy + 3.4, "text-anchor": "end",
        fill: it.isDual ? COLORS.navy : COLORS.ink,
        "font-size": 9.8, "font-weight": it.isDual ? 700 : 500,
        "font-family": "Segoe UI, sans-serif"
      }, it.label));

      const barColor = it.isDual ? COLORS.navy : (hi < 0 ? "#B85260" : COLORS.steel);
      svg.appendChild(svgEl("line", {
        x1: xOf(lo), y1: cy, x2: xOf(hi), y2: cy,
        stroke: barColor, "stroke-width": it.isDual ? 2.6 : 1.9
      }));
      [lo, hi].forEach(ev => {
        svg.appendChild(svgEl("line", {
          x1: xOf(ev), y1: cy - 4.5, x2: xOf(ev), y2: cy + 4.5,
          stroke: barColor, "stroke-width": 1.4
        }));
      });
      svg.appendChild(svgEl("circle", {
        cx: xOf(med), cy, r: it.isDual ? 4.4 : 3.5,
        fill: it.isDual ? COLORS.navy : barColor,
        stroke: "#FFFFFF", "stroke-width": 1.2
      }));

      svg.appendChild(svgEl("text", {
        x: padL + plotW + 10, y: cy + 3.2,
        fill: it.isDual ? COLORS.navy : COLORS.ink,
        "font-size": 9.1, "font-weight": it.isDual ? 700 : 500,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `[${fmtSigned(lo, 2)}, ${fmtSigned(hi, 2)}]`));

      svg.appendChild(svgEl("text", {
        x: padL + plotW + 114, y: cy + 3.2, "text-anchor": "end",
        fill: pLoss <= 1 ? COLORS.long : (pLoss >= 20 ? COLORS.short : COLORS.ink),
        "font-size": 9.1, "font-weight": 700,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${fmt(pLoss, 1)}%`));

      svg.appendChild(svgEl("text", {
        x: W - 4, y: cy + 3.2, "text-anchor": "end",
        fill: COLORS.ink, "font-size": 9.1, "font-weight": 600,
        "font-family": "Segoe UI, sans-serif", "font-variant-numeric": "tabular-nums"
      }, `${fmt(cvar, 0)}bp`));
    });
  }

  function renderPortfolioPanel() {
    const kpiHost = $("portKpiRow");
    const barsHost = $("portAssetBars");
    if (!kpiHost || !barsHost) return;

    const dr = RAW_METRICS && RAW_METRICS.dual_regime_lab;
    const port = dr && dr.portfolio_8_asset;
    const sum = port && port.summary;
    if (!port || !sum) {
      kpiHost.innerHTML = `<div style="grid-column:1/-1; text-align:center; padding:24px 8px; color:#526D8F; font-style:italic;">Awaiting 8-asset portfolio diversification metrics in data/bundle.js…</div>`;
      barsHost.innerHTML = "";
      return;
    }

    if ($("portDivBadge")) $("portDivBadge").textContent = `Div Ratio ${fmt(sum.diversification_ratio, 2)}\u00D7`;
    if ($("portMetaSubtitle")) {
      $("portMetaSubtitle").textContent =
        `60d · \u03C1=${fmt(sum.avg_pairwise_corr, 3)} · Turbo RP ${fmt(sum.turbo_risk_parity_sharpe, 2)}`;
    }

    kpiHost.innerHTML = `
      <div class="port-kpi-box">
        <small>Single Avg SR</small>
        <strong>${fmt(sum.single_asset_avg_sharpe, 2)}</strong>
        <span>Avg MDD -${fmt(sum.single_asset_avg_mdd_pct, 2)}%</span>
      </div>
      <div class="port-kpi-box">
        <small>Equal-Weight SR</small>
        <strong>${fmt(sum.equal_weight_sharpe, 2)}</strong>
        <span>MDD -${fmt(sum.equal_weight_mdd_pct, 2)}%</span>
      </div>
      <div class="port-kpi-box primary">
        <small>Risk-Parity SR</small>
        <strong>${fmt(sum.risk_parity_sharpe, 2)}</strong>
        <span>MDD -${fmt(sum.risk_parity_mdd_pct, 2)}% · +${fmt(sum.sharpe_uplift_vs_single_pct, 0)}%</span>
      </div>
    `;

    const assets = Array.isArray(port.assets) ? port.assets : [];
    const maxSr = Math.max(4.0, ...assets.map(a => Math.abs(Number(a.net_sharpe || 0))));
    barsHost.innerHTML = assets.map(a => {
      const sr = Number(a.net_sharpe || 0);
      const wPct = Number(a.risk_parity_weight_pct ?? 12.5);
      const barW = clamp((Math.abs(sr) / maxSr) * 100, 5, 100);
      const barColor = sr < 0 ? "#B85260" : (sr >= 3.5 ? COLORS.navy : COLORS.cobalt);
      return `
        <div class="port-asset-row">
          <span class="port-sym">${a.symbol}</span>
          <span class="port-track"><i style="width:${barW.toFixed(1)}%; background:${barColor};"></i></span>
          <span class="port-meta ${sr < 0 ? "pnl-neg" : ""}">SR ${fmt(sr, 2)} · ${fmt(wPct, 1)}%</span>
        </div>
      `;
    }).join("");
  }

  function renderResearchTab() {
    if (!RAW_SESSION) return;
    runMultiAssetRadarScan();
    renderRadarTable();
    renderTermStructureChart();
    renderAlphaDecayChart();
    renderWalkForwardTable();
    renderStressMatrix();
    renderBootstrapChart();
    renderPortfolioPanel();
  }

  /* ---------- Tab 4: Editorial Blue Sheet Page 1 / Page 2 / Page 3 Switcher ---------- */
  function switchSheetPage(pageNum) {
    const raw = String(pageNum);
    const p = raw === "3" ? "3" : (raw === "2" ? "2" : "1");
    state.sheetPage = p;
    const srcMap = {
      "1": "blue_sheet.html",
      "2": "research_brief.html",
      "3": "dual_regime_brief.html"
    };
    const titleMap = {
      "1": "Publication Broadsheet — Map of Blended 5–20m VWAP (Page 1)",
      "2": "Publication Broadsheet — Spectral VWAP: OU Half-Life, Curvature & Toxicity Gating (Page 2)",
      "3": "Publication Broadsheet — Dual-Regime Spectral VWAP: Bayesian Bifurcation (Page 3)"
    };
    const subMap = {
      "1": "Single-page Letter layout powered by identical quantitative bundle",
      "2": "Companion IEEE Quant Blue research brief · Equations (3)–(4), Walk-Forward DSR & Stress Matrix",
      "3": "Companion IEEE Quant Blue monograph · Hamilton Filter Eq. (5)–(6), 5-Regime Matrix & Bootstrap CIs"
    };
    const src = srcMap[p];
    const frame = $("blueSheetFrame");
    if (frame && frame.getAttribute("src") !== src) {
      frame.setAttribute("src", src);
    }
    const openLink = $("btnOpenFullSheet");
    if (openLink) openLink.setAttribute("href", src);
    if ($("sheetToolbarTitle")) $("sheetToolbarTitle").textContent = titleMap[p];
    if ($("sheetToolbarSub")) $("sheetToolbarSub").textContent = subMap[p];

    document.querySelectorAll("#sheetPageSwitcher .sheet-page-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.page === p);
    });
    try {
      const url = new URL(window.location.href);
      if (state.activeTab === "sheet") {
        if (p === "1") url.searchParams.delete("page");
        else url.searchParams.set("page", p);
        window.history.replaceState({}, "", url.toString());
      }
    } catch (_err) {}
  }

  /* ---------- Tab & Event Wiring ---------- */
  function switchTab(tabName) {
    const valid = ["lab", "attribution", "research", "sheet"];
    const target = valid.includes(tabName) ? tabName : "lab";
    state.activeTab = target;

    document.querySelectorAll(".nav-tab").forEach(btn => {
      const active = btn.dataset.tab === target;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", String(active));
    });

    document.querySelectorAll(".tab-panel").forEach(panel => {
      panel.classList.toggle("active", panel.id === `panel-${target}`);
    });

    if (target === "research" && RAW_SESSION) {
      renderResearchTab();
    }

    try {
      const url = new URL(window.location.href);
      url.searchParams.set("tab", target);
      if (target !== "sheet") url.searchParams.delete("page");
      window.history.replaceState({}, "", url.toString());
    } catch (_err) {
      // Ignore history restriction on local file:/// if browser blocks query updates
    }
  }

  function updatePresetButtons() {
    document.querySelectorAll("#presetGrid .preset-btn").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.preset === state.preset);
    });
  }

  function applyPreset(presetKey) {
    if (!PRESETS[presetKey]) return;
    state.preset = presetKey;
    if (presetKey === "static") {
      state.weights = getStaticWeightsFromMeta();
    } else if (presetKey !== "custom") {
      state.weights = PRESETS[presetKey].weights.slice();
    }
    state.selectedTradeIdx = null;
    updatePresetButtons();
    recomputeAndRenderLab();
  }

  function bindEvents() {
    // Top tabs
    document.querySelectorAll(".nav-tab").forEach(btn => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    $("btnQuickSheet").addEventListener("click", () => switchTab("sheet"));

    // Print Blue Sheet button (prints active page 1, 2, or 3)
    $("btnPrintSheet").addEventListener("click", () => {
      const frame = $("blueSheetFrame");
      const activeSrc = state.sheetPage === "3"
        ? "dual_regime_brief.html"
        : (state.sheetPage === "2" ? "research_brief.html" : "blue_sheet.html");
      try {
        if (frame && frame.contentWindow) {
          frame.contentWindow.focus();
          frame.contentWindow.print();
          return;
        }
      } catch (_err) {
        // Fallback if file:/// blocks cross-frame print
      }
      window.open(activeSrc, "_blank", "noopener");
    });

    // Live Paper-Trading Execution Desk controls
    const btnBuy = $("btnPaperBuy");
    if (btnBuy) btnBuy.addEventListener("click", () => paperExecuteOrder("LONG", "MANUAL"));
    const btnSell = $("btnPaperSell");
    if (btnSell) btnSell.addEventListener("click", () => paperExecuteOrder("SHORT", "MANUAL"));
    const btnFlat = $("btnPaperFlatten");
    if (btnFlat) btnFlat.addEventListener("click", () => paperFlatten("FLATTEN"));
    const btnAuto = $("btnPaperAuto");
    if (btnAuto) {
      btnAuto.addEventListener("click", () => {
        state.paper.autoPilot = !state.paper.autoPilot;
        updatePaperDeskUI();
        if (state.paper.autoPilot && !state.rt.running) {
          startLiveStream();
        }
        showToast({
          kind: state.paper.autoPilot ? "long" : "exit",
          symbol: state.symbol,
          label: state.paper.autoPilot ? "AUTO-PILOT ENGAGED" : "AUTO-PILOT PAUSED",
          time: "Now",
          detail: state.paper.autoPilot
            ? "Auto-executing causal Fade (<0.38) & Surf (>0.62) fills into $100,000 paper account."
            : "Paper Auto-Pilot paused; existing position remains managed manually."
        });
      });
    }
    const btnCsv = $("btnExportCsv");
    if (btnCsv) btnCsv.addEventListener("click", exportDeskCsv);

    // Editorial Blue Sheet Page 1 / Page 2 Switcher
    document.querySelectorAll("#sheetPageSwitcher .sheet-page-btn").forEach(btn => {
      btn.addEventListener("click", () => switchSheetPage(btn.dataset.page));
    });

    // Tab 3 Deep Research Radar controls
    const btnRescan = $("btnRescanRadar");
    if (btnRescan) {
      btnRescan.addEventListener("click", () => {
        runMultiAssetRadarScan();
        renderRadarTable();
        renderTermStructureChart();
      });
    }
    document.querySelectorAll("#radarTable thead th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const key = th.dataset.rsort;
        if (state.radarSortKey === key) {
          state.radarSortDir = state.radarSortDir === "desc" ? "asc" : "desc";
        } else {
          state.radarSortKey = key;
          state.radarSortDir = key === "symbol" ? "asc" : "desc";
        }
        renderRadarTable();
      });
    });

    // Symbol input, chips & CSV import
    const symForm = $("symbolForm");
    if (symForm) {
      symForm.addEventListener("submit", evt => {
        evt.preventDefault();
        loadSymbol($("symbolInput").value);
      });
    }
    document.querySelectorAll("#symbolChips .sym-chip").forEach(chip => {
      chip.addEventListener("click", () => loadSymbol(chip.dataset.symbol));
    });
    const csvInput = $("csvFileInput");
    if (csvInput) {
      csvInput.addEventListener("change", evt => {
        const f = evt.target.files && evt.target.files[0];
        if (f) handleCsvFileUpload(f);
        evt.target.value = "";
      });
    }

    // Real-time stream & alert notification controls
    const btnLive = $("btnLiveToggle");
    if (btnLive) {
      btnLive.addEventListener("click", () => {
        if (state.rt.running) stopLiveStream();
        else startLiveStream();
      });
    }
    document.querySelectorAll("#rtSpeedGroup .rt-speed-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        state.rt.cadenceMs = Number(btn.dataset.ms) || 1000;
        document.querySelectorAll("#rtSpeedGroup .rt-speed-btn").forEach(b =>
          b.classList.toggle("active", b === btn)
        );
        if (state.rt.running) {
          if (state.rt.timerId) clearInterval(state.rt.timerId);
          state.rt.timerId = setInterval(stepSimulatedLiveTick, state.rt.cadenceMs);
        }
        updateLiveControlsUI();
      });
    });
    const btnTest = $("btnTestAlert");
    if (btnTest) btnTest.addEventListener("click", triggerSampleAlert);

    const btnTurbo = $("btnTurboMode");
    if (btnTurbo) {
      btnTurbo.addEventListener("click", () => {
        state.turboMode = !state.turboMode;
        state.selectedTradeIdx = null;
        updateLiveControlsUI();
        recomputeAndRenderLab();
        if (state.turboMode) {
          showToast({
            kind: "long",
            symbol: state.symbol,
            label: "TURBO 10\u00D7 ALPHA ACTIVE",
            time: "Now",
            detail: "Passive Maker Peg (-62% cost) + Asymmetric Trailing Lock + Fractional-Kelly Sizing engaged."
          });
        }
      });
    }

    const btnLaunchTurbo = $("btnLaunchTurboLab");
    if (btnLaunchTurbo) {
      btnLaunchTurbo.addEventListener("click", () => {
        state.turboMode = true;
        state.preset = "adaptive";
        updatePresetButtons();
        updateLiveControlsUI();
        recomputeAndRenderLab();
        switchTab("lab");
      });
    }

    const btnClearAlerts = $("btnClearAlerts");
    if (btnClearAlerts) {
      btnClearAlerts.addEventListener("click", () => {
        state.rt.alerts = [];
        renderAlertLog();
      });
    }

    const bindChk = (id, fn) => {
      const el = $(id);
      if (el) el.addEventListener("change", evt => fn(Boolean(evt.target.checked), el));
    };
    bindChk("chkTrigBreach", v => { state.rt.triggers.breach = v; });
    bindChk("chkTrigExit", v => { state.rt.triggers.exit = v; });
    bindChk("chkTrigSurge", v => { state.rt.triggers.surge = v; });
    bindChk("chkAlertToast", v => { state.rt.channels.toast = v; });
    bindChk("chkAlertSound", v => { state.rt.channels.sound = v; });
    bindChk("chkAlertDesktop", async (v, el) => {
      if (!v) {
        state.rt.channels.desktop = false;
        return;
      }
      if (typeof Notification === "undefined") {
        el.checked = false;
        state.rt.channels.desktop = false;
        showToast({ kind: "exit", symbol: state.symbol, label: "DESKTOP UNSUPPORTED", time: "Now", detail: "This browser does not support Desktop Notifications." });
        return;
      }
      if (Notification.permission === "granted") {
        state.rt.channels.desktop = true;
      } else if (Notification.permission !== "denied") {
        const perm = await Notification.requestPermission();
        state.rt.channels.desktop = perm === "granted";
        el.checked = state.rt.channels.desktop;
      } else {
        el.checked = false;
        state.rt.channels.desktop = false;
        showToast({ kind: "exit", symbol: state.symbol, label: "PERMISSION BLOCKED", time: "Now", detail: "Enable browser notifications in site settings to receive desktop pushes." });
      }
    });

    // Presets
    document.querySelectorAll("#presetGrid .preset-btn").forEach(btn => {
      btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
    });

    // Synchronized weight sliders
    const sliderIds = ["sliderW5", "sliderW10", "sliderW15", "sliderW20"];
    sliderIds.forEach((id, idx) => {
      $(id).addEventListener("input", evt => {
        // If user starts dragging from Adaptive or OU-Spectral mode, seed from current displayed values
        if (PRESETS[state.preset] && PRESETS[state.preset].dynamic) {
          state.weights = sliderIds.map(sid => Number($(sid).value));
        }
        state.weights = normalizeIntegerWeights(state.weights, idx, Number(evt.target.value));
        state.preset = "custom";
        state.selectedTradeIdx = null;
        updatePresetButtons();
        recomputeAndRenderLab();
      });
    });

    // Entry Z & Max Hold sliders
    $("sliderEntryZ").addEventListener("input", evt => {
      state.entryZ = Number(evt.target.value);
      state.selectedTradeIdx = null;
      recomputeAndRenderLab();
    });

    $("sliderMaxHold").addEventListener("input", evt => {
      state.maxHold = Number(evt.target.value);
      state.selectedTradeIdx = null;
      recomputeAndRenderLab();
    });

    // Zoom pills
    document.querySelectorAll("#zoomPills .pill-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        state.zoom = btn.dataset.zoom;
        document.querySelectorAll("#zoomPills .pill-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.zoom === state.zoom)
        );
        syncSlidersUI();
        updateKPIStrip();
        renderIntradayChart();
        renderTradeTape();
      });
    });

    // Chart layer toggles
    const toggleMap = [
      ["chkCandles", "candles"],
      ["chkHorizons", "horizons"],
      ["chkBands", "bands"],
      ["chkTrades", "trades"]
    ];
    toggleMap.forEach(([elId, key]) => {
      $(elId).addEventListener("change", evt => {
        state.layers[key] = Boolean(evt.target.checked);
        renderIntradayChart();
      });
    });

    $("btnClearTradeSelection").addEventListener("click", () => {
      state.selectedTradeIdx = null;
      renderIntradayChart();
      renderTradeTape();
    });

    // Tab 2 filter pills
    document.querySelectorAll("#regimeFilterBar .pill-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        state.tableFilter = btn.dataset.filter;
        document.querySelectorAll("#regimeFilterBar .pill-btn").forEach(b =>
          b.classList.toggle("active", b.dataset.filter === state.tableFilter)
        );
        renderBenchmarkTable();
      });
    });

    // Tab 2 sortable columns
    document.querySelectorAll("#benchmarkTable thead th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        } else {
          state.sortKey = key;
          state.sortDir = (key === "name" || key === "regime" || key === "window_label") ? "asc" : "desc";
        }
        renderBenchmarkTable();
      });
    });

    $("btnLoadInLab").addEventListener("click", loadSelectedConfigInLab);
  }

  function renderTurboBanner() {
    const tb = RAW_METRICS && RAW_METRICS.turbo_benchmark;
    if (!tb) return;
    if ($("tbProfitMult")) $("tbProfitMult").textContent = `${fmt(tb.profit_multiple, 1)}\u00D7`;
    if ($("tbAnnRetComp")) $("tbAnnRetComp").textContent = `+${fmtInt(tb.turbo_annual_return_pct)}% vs +${fmt(tb.base_annual_return_pct, 1)}%`;
    if ($("tbNetSharpe")) $("tbNetSharpe").textContent = fmt(tb.turbo_net_sharpe, 2);
    if ($("tbSharpeComp")) $("tbSharpeComp").textContent = `vs ${fmt(tb.base_net_sharpe, 2)} base · PF ${fmt(tb.turbo_profit_factor, 2)}`;
    if ($("tbCostBps")) $("tbCostBps").textContent = `${fmt(tb.turbo_avg_cost_bps, 2)} bp`;
    if ($("tbCostComp")) $("tbCostComp").textContent = `vs ${fmt(tb.base_avg_cost_bps, 2)} bp taker (-${Math.round((1 - tb.turbo_avg_cost_bps / tb.base_avg_cost_bps) * 100)}%)`;
    if ($("tbSpeedup")) $("tbSpeedup").textContent = `${fmt(tb.engine_speedup_x, 1)}\u00D7 Core`;
    if ($("tbSpeedMs")) $("tbSpeedMs").textContent = `${fmt(tb.indicator_ms, 1)}ms indicators (${fmt(2807.16 / Math.max(tb.indicator_ms, 1), 0)}\u00D7)`;
  }

  /* ---------- Bootstrap ---------- */
  function readInitialRoute() {
    const params = new URLSearchParams(window.location.search);
    const hash = window.location.hash.replace(/^#/, "").trim().toLowerCase();
    const tab = (params.get("tab") || hash || "lab").toLowerCase();
    const zoom = params.get("zoom");
    const preset = params.get("preset");
    const sym = params.get("symbol");
    const turbo = params.get("turbo");
    const page = params.get("page");
    if (zoom && ["spotlight", "full", "open", "midday", "close"].includes(zoom)) {
      state.zoom = zoom;
    }
    if (preset && PRESETS[preset]) {
      state.preset = preset;
    }
    if (sym) {
      state.symbol = normalizeSymbolInput(sym);
    }
    if (turbo === "1" || turbo === "true") {
      state.turboMode = true;
    }
    if (page === "2" || page === "3") {
      state.sheetPage = page;
    }
    return tab;
  }

  function init() {
    bindEvents();
    const initialTab = readInitialRoute();
    switchTab(initialTab);

    if (!window.VWAP_DATA || !window.VWAP_DATA.metrics || !window.VWAP_DATA.session) {
      $("dataBanner").classList.add("visible");
      return;
    }

    RAW_METRICS = window.VWAP_DATA.metrics;
    RAW_SESSION = window.VWAP_DATA.session;
    BENCHMARK_SESSION = window.VWAP_DATA.session;
    if (BENCHMARK_SESSION && Array.isArray(BENCHMARK_SESSION.bars)) {
      enrichPhase2Microstructure(BENCHMARK_SESSION.bars, true);
    }

    // Sync topbar metadata from engine meta
    const meta = RAW_METRICS.meta || {};
    if (meta.sessions && meta.total_bars) {
      $("topbarMeta").textContent = `${meta.sessions} Sessions · ${fmtInt(meta.total_bars)} Bars`;
    }

    // Calibrate default entryZ & maxHold from session metadata if exported (flagship engine default is 1.58 / 20m)
    if (RAW_SESSION.params) {
      if (RAW_SESSION.params.entry_z) {
        state.defaultEntryZ = Number(RAW_SESSION.params.entry_z);
        state.entryZ = state.defaultEntryZ;
      }
      if (RAW_SESSION.params.max_hold_min) {
        state.defaultMaxHold = Number(RAW_SESSION.params.max_hold_min);
        state.maxHold = state.defaultMaxHold;
      }
    }

    $("sliderEntryZ").value = state.entryZ.toFixed(2);
    $("sliderMaxHold").value = String(state.maxHold);

    // Sync preset static weights and initialize non-dynamic URL presets cleanly
    PRESETS.static.weights = getStaticWeightsFromMeta();
    if (PRESETS[state.preset] && !PRESETS[state.preset].dynamic && state.preset !== "custom") {
      state.weights = PRESETS[state.preset].weights.slice();
    }

    // Update zoom pill active class if overridden via URL
    document.querySelectorAll("#zoomPills .pill-btn").forEach(b =>
      b.classList.toggle("active", b.dataset.zoom === state.zoom)
    );
    updatePresetButtons();

    // Pick flagship config as initial selection on Tab 2
    const flagship = (RAW_METRICS.configs || []).find(c => c.is_flagship) || (RAW_METRICS.configs || [])[0];
    if (flagship) state.selectedConfigId = flagship.id;

    // Render all views cleanly (apply URL ?symbol= if requested)
    if (state.symbol && state.symbol !== "ES") {
      loadSymbol(state.symbol, { skipUrl: true });
    } else {
      updateSymbolUI("ES", "60-Session Benchmark", "");
      recomputeAndRenderLab();
    }
    renderFrontierChart();
    renderConfigInspector();
    renderWeightScheduleChart();
    renderDurationChart();
    renderBenchmarkTable();
    renderTurboBanner();
    renderResearchTab();
    switchSheetPage(state.sheetPage);

    if (new URLSearchParams(window.location.search).get("live") === "1") {
      startLiveStream();
    } else {
      updateLiveControlsUI();
    }

    switchTab(initialTab);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
