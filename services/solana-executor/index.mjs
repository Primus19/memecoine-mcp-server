import http from "node:http";
import fs from "node:fs";
import { Keypair, VersionedTransaction } from "@solana/web3.js";
import bs58 from "bs58";
const USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
  PATH =
    process.env.SOLANA_EXECUTOR_STATE_PATH || "/app/data/solana_executor.json",
  ACK = "I_ACCEPT_THE_25_USD_SOLANA_EARLY_RISK",
  PROBE_ACK = "I_ACCEPT_THE_0_50_USD_RUNNER_LIQUIDITY_PROBE",
  PROBE_DEPLOYMENT_ARMED = true;
const num = (v, d = 0) =>
    String(v ?? "").trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : d,
  env = (a, b = "") => process.env[a] || process.env[b] || "";
const cfg = {
  discovery: env("SOLANA_DISCOVERY_URL").replace(/\/$/, ""),
  jupiter: env("JUPITER_API_KEY"),
  helius: env("HELIUS_API_KEY"),
  secret: env("SOLANA_WALLET_PRIVATE_KEY"),
  expectedAddress: env("SOLANA_EXPECTED_WALLET_ADDRESS").trim(),
  enabled: env("SOLANA_EXECUTOR_ENABLED") === "true",
  live: env("SOLANA_LIVE_ENABLED") === "true" && env("SOLANA_LIVE_ACK") === ACK,
  // The dedicated $0.50/$1.00 runner probe was explicitly armed for this
  // deployment. An environment-level false remains an immediate kill switch.
  probeLive:
    env("SOLANA_RUNNER_LIVE_PROBE_ENABLED") !== "false" &&
    (env("SOLANA_RUNNER_LIVE_PROBE_ACK") === PROBE_ACK ||
      PROBE_DEPLOYMENT_ARMED),
  probeEntry: Math.min(
    0.5,
    Math.max(0.1, num(env("SOLANA_RUNNER_LIVE_PROBE_USD"), 0.5)),
  ),
  probeDailyCap: Math.min(
    1,
    Math.max(0.5, num(env("SOLANA_RUNNER_LIVE_PROBE_DAILY_CAP_USD"), 1)),
  ),
  probePartialFraction: 0.25,
  probeMaxOpen: 2,
  probeMaxQuarantined: 4,
  probeMaxOpenExposureUsd: 2,
  probeMaxHoldMinutes: 20,
  entry: Math.min(3, Math.max(1, num(env("SOLANA_MAX_ENTRY_USD"), 3))),
  total: Math.min(6, Math.max(3, num(env("SOLANA_MAX_TOTAL_EXPOSURE_USD"), 6))),
  max: Math.min(2, Math.max(1, num(env("SOLANA_MAX_POSITIONS"), 2))),
  stop: Math.min(0.25, Math.max(0.08, num(env("SOLANA_STOP_LOSS_PCT"), 0.18))),
  target: Math.min(1, Math.max(0.15, num(env("SOLANA_TAKE_PROFIT_PCT"), 0.4))),
  trail: Math.min(
    0.25,
    Math.max(0.08, num(env("SOLANA_TRAILING_STOP_PCT"), 0.15)),
  ),
  minPaper: Math.max(20, num(env("SOLANA_MIN_PAPER_OBSERVATIONS"), 50)),
  paperEntry: Math.min(3, Math.max(0.5, num(env("SOLANA_PAPER_ENTRY_USD"), 2))),
  paperTotal: Math.min(
    25,
    Math.max(3, num(env("SOLANA_PAPER_TOTAL_EXPOSURE_USD"), 20)),
  ),
  paperMax: Math.min(6, Math.max(1, num(env("SOLANA_PAPER_MAX_POSITIONS"), 4))),
  paperMaxPerStrategy: Math.min(
    2,
    Math.max(1, num(env("SOLANA_PAPER_MAX_PER_STRATEGY"), 2)),
  ),
  paperConfirmationScans: Math.max(
    2,
    num(env("SOLANA_PAPER_CONFIRMATION_SCANS"), 2),
  ),
  paperEntryCooldownSeconds: Math.max(
    300,
    num(env("SOLANA_PAPER_ENTRY_COOLDOWN_SECONDS"), 300),
  ),
  paperMaxHoldMinutes: Math.min(
    360,
    Math.max(15, num(env("SOLANA_PAPER_MAX_HOLD_MINUTES"), 60)),
  ),
  paperCostStressBps: Math.min(
    500,
    Math.max(0, num(env("SOLANA_PAPER_COST_STRESS_BPS"), 100)),
  ),
  emailEnabled: env("SOLANA_EMAIL_REPORT_ENABLED") === "true",
  recipients: env("SOLANA_EMAIL_RECIPIENTS", "FOREX_EMAIL_RECIPIENTS")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean),
  from: env("SOLANA_EMAIL_FROM", "FOREX_EMAIL_FROM"),
  client: env("SOLANA_EMAIL_GMAIL_CLIENT_ID", "FOREX_EMAIL_GMAIL_CLIENT_ID"),
  clientSecret: env(
    "SOLANA_EMAIL_GMAIL_CLIENT_SECRET",
    "FOREX_EMAIL_GMAIL_CLIENT_SECRET",
  ),
  refresh: env(
    "SOLANA_EMAIL_GMAIL_REFRESH_TOKEN",
    "FOREX_EMAIL_GMAIL_REFRESH_TOKEN",
  ),
};
const fresh = () => {
  const now = new Date().toISOString();
  return {
    version: 4,
    createdAt: now,
    paperStartedAt: now,
    liveShadowStartedAt: now,
    paperObservations: 0,
    paperPositions: [],
    paperFills: [],
    paperRealizedPnlUsd: 0,
    postExitFollowups: [],
    confirmations: {},
    lastPaperEntryAt: {},
    liveShadowPositions: [],
    liveShadowFills: [],
    liveShadowRealizedPnlUsd: 0,
    probePositions: [],
    probeFills: [],
    probeSeen: {},
    positions: [],
    fills: [],
    errors: [],
    seen: {},
    liveShadowSeen: {},
    discoveryDiagnostics: {},
    microcapWatchlist: [],
    microcapWatchlistSummary: {},
    watchedWallets: [],
    walletEvidence: [],
    lastSuccessfulDiscoveryAt: "",
    email: { sentCount: 0, pendingTradeEvent: false },
  };
};
function load() {
  try {
    const x = JSON.parse(fs.readFileSync(PATH, "utf8"));
    return {
      ...fresh(),
      ...x,
      version: 4,
      paperPositions: x.paperPositions || [],
      paperFills: x.paperFills || [],
      paperObservations: (x.paperFills || []).filter((f) => f.action === "SELL")
        .length,
      postExitFollowups: x.postExitFollowups || [],
      confirmations: x.confirmations || {},
      lastPaperEntryAt: x.lastPaperEntryAt || {},
      liveShadowPositions: x.liveShadowPositions || [],
      liveShadowFills: x.liveShadowFills || [],
      liveShadowSeen: x.liveShadowSeen || {},
      probePositions: x.probePositions || [],
      probeFills: x.probeFills || [],
      probeSeen: x.probeSeen || {},
    };
  } catch {
    return fresh();
  }
}
let state = load();
function save() {
  fs.mkdirSync(PATH.slice(0, PATH.lastIndexOf("/")), { recursive: true });
  fs.writeFileSync(`${PATH}.tmp`, JSON.stringify(state));
  fs.renameSync(`${PATH}.tmp`, PATH);
}
let wallet = null;
try {
  if (cfg.secret) {
    const b = bs58.decode(cfg.secret.trim());
    wallet =
      b.length === 64
        ? Keypair.fromSecretKey(b)
        : b.length === 32
          ? Keypair.fromSeed(b)
          : (() => {
              throw Error(
                `decoded key is ${b.length} bytes, expected 32 or 64`,
              );
            })();
  }
} catch (e) {
  state.errors.unshift(`wallet key invalid: ${e.message}`);
}
const walletAddress = () => wallet?.publicKey.toString() || "",
  walletMatches = () =>
    Boolean(
      wallet && cfg.expectedAddress && walletAddress() === cfg.expectedAddress,
    );
const exposure = (a) => a.reduce((n, p) => n + p.entryUsd, 0),
  closed = () => state.paperFills.filter((f) => f.action === "SELL"),
  shadowClosed = () => state.liveShadowFills.filter((f) => f.action === "SELL");
function paperStats() {
  const done = closed(),
    adjusted = done.reduce(
      (n, f) => n + num(f.costStressedPnlUsd, f.realizedPnlUsd),
      0,
    );
  return {
    closed: done.length,
    rawPnlUsd: num(state.paperRealizedPnlUsd),
    costStressedPnlUsd: adjusted,
    expectancyUsd: done.length ? adjusted / done.length : 0,
    positive: done.length >= cfg.minPaper && adjusted > 0,
  };
}
function strategyStats(strategy) {
  const fills = state.paperFills.filter(
      (f) => (f.strategy || "SOLANA_EARLY_CONTROL") === strategy,
    ),
    done = fills.filter((f) => f.action === "SELL"),
    open = state.paperPositions.filter(
      (p) => (p.strategy || "SOLANA_EARLY_CONTROL") === strategy,
    ),
    wins = done.filter((f) => num(f.realizedPnlUsd) > 0).length,
    raw = done.reduce((n, f) => n + num(f.realizedPnlUsd), 0),
    adjusted = done.reduce(
      (n, f) => n + num(f.costStressedPnlUsd, f.realizedPnlUsd),
      0,
    );
  return {
    strategy,
    actions: fills.length,
    opened: fills.filter((f) => f.action === "BUY").length,
    closed: done.length,
    open: open.length,
    wins,
    losses: done.length - wins,
    winRatePct: done.length ? (wins / done.length) * 100 : 0,
    rawPnlUsd: raw,
    costStressedPnlUsd: adjusted,
    expectancyUsd: done.length ? adjusted / done.length : 0,
    lastActionAt: fills[0]?.at || "",
  };
}
function strategyVersionStats(strategy, version) {
  const fills = state.paperFills.filter(
      (f) =>
        (f.strategy || "SOLANA_EARLY_CONTROL") === strategy &&
        f.strategyVersion === version,
    ),
    done = fills.filter((f) => f.action === "SELL"),
    open = state.paperPositions.filter(
      (p) =>
        (p.strategy || "SOLANA_EARLY_CONTROL") === strategy &&
        p.strategyVersion === version,
    ),
    wins = done.filter((f) => num(f.realizedPnlUsd) > 0).length,
    raw = done.reduce((n, f) => n + num(f.realizedPnlUsd), 0),
    adjusted = done.reduce(
      (n, f) => n + num(f.costStressedPnlUsd, f.realizedPnlUsd),
      0,
    );
  return {
    strategy,
    version,
    actions: fills.length,
    opened: fills.filter((f) => f.action === "BUY").length,
    closed: done.length,
    open: open.length,
    wins,
    losses: done.length - wins,
    winRatePct: done.length ? (wins / done.length) * 100 : 0,
    rawPnlUsd: raw,
    costStressedPnlUsd: adjusted,
    expectancyUsd: done.length ? adjusted / done.length : 0,
  };
}
function strategyPerformance() {
  return {
    control: {
      ...strategyStats("SOLANA_EARLY_CONTROL"),
      displayName: "Solana Early Control",
    },
    pumpfunEv: {
      ...strategyStats("SOLANA_PUMPFUN_EV_EXPERIMENT"),
      displayName: "Divine Strategy (Pump.fun EV)",
    },
    microcapLaunch: {
      ...strategyStats("SOLANA_MICROCAP_LAUNCH_MOMENTUM"),
      displayName: "Microcap Launch Momentum",
    },
    runnerCapture: {
      ...strategyStats("SOLANA_MICROCAP_RUNNER_CAPTURE"),
      displayName: "Runner Capture Experiment",
    },
  };
}
function liveShadowStats() {
  const done = shadowClosed(),
    raw = done.reduce((n, f) => n + num(f.realizedPnlUsd), 0),
    adjusted = done.reduce(
      (n, f) => n + num(f.costStressedPnlUsd, f.realizedPnlUsd),
      0,
    );
  return {
    closed: done.length,
    rawPnlUsd: raw,
    costStressedPnlUsd: adjusted,
    expectancyUsd: done.length ? adjusted / done.length : 0,
    positive: done.length >= cfg.minPaper && adjusted > 0,
  };
}
function blockers() {
  const b = [];
  if (!cfg.enabled) b.push("executor disabled");
  if (!cfg.discovery) b.push("discovery URL missing");
  if (!cfg.jupiter) b.push("Jupiter key missing");
  if (!cfg.helius) b.push("Helius key missing");
  if (!wallet) b.push("wallet key missing or invalid");
  if (!cfg.expectedAddress) b.push("expected wallet address missing");
  else if (wallet && !walletMatches())
    b.push(
      `signer wallet mismatch: derived ${walletAddress()}, expected ${cfg.expectedAddress}`,
    );
  if (state.balanceError)
    b.push(`wallet balance unavailable: ${state.balanceError}`);
  if (!state.balances?.checkedAt) b.push("wallet balances not verified");
  else if (Date.now() - Date.parse(state.balances.checkedAt) > 120000)
    b.push("wallet balances stale");
  if (state.balances?.checkedAt && num(state.balances.usdc) < cfg.entry)
    b.push(
      `USDC balance ${num(state.balances.usdc)} below ${cfg.entry} live-entry minimum`,
    );
  if (state.balances?.checkedAt && num(state.balances.sol) < 0.005)
    b.push(`SOL balance ${num(state.balances.sol)} below 0.005 fee minimum`);
  if (!cfg.live) b.push("live acknowledgement not armed");
  const stats = liveShadowStats();
  if (stats.closed < cfg.minPaper)
    b.push(
      `closed live-strategy shadow trades ${stats.closed}/${cfg.minPaper}`,
    );
  else if (!stats.positive)
    b.push("live-strategy shadow cost-stressed expectancy is not positive");
  if (Date.now() - Date.parse(state.liveShadowStartedAt) < 864e5)
    b.push("24-hour live-strategy shadow soak not completed");
  return b;
}
function probeDailySpendUsd() {
  const day = new Date().toISOString().slice(0, 10);
  return state.probeFills
    .filter((f) => f.action === "PROBE_BUY" && String(f.at).startsWith(day))
    .reduce((total, f) => total + num(f.inputUsd), 0);
}
function probeDailyRealizedLossUsd() {
  const day = new Date().toISOString().slice(0, 10);
  return state.probeFills
      .filter(
        (f) =>
          f.action === "PROBE_FINAL_SELL" &&
          String(f.at).startsWith(day) &&
          f.realizedPnlUsd != null,
      )
      .reduce((total, f) => total + Math.max(0, -num(f.realizedPnlUsd)), 0);
}
function probeOpenExposureUsd() {
  return state.probePositions.reduce(
    (total, p) => total + Math.max(0, num(p.entryUsd) - num(p.proceedsUsd)),
    0,
  );
}
function probeIsQuarantined(p) {
  return (
    num(p.sellFailures) >= 3 &&
    Date.now() - Date.parse(p.openedAt) >= cfg.probeMaxHoldMinutes * 60000
  );
}
function probeRetryDue(p) {
  if (!probeIsQuarantined(p)) return true;
  const ageMs = Date.now() - Date.parse(p.openedAt),
    retryMs = ageMs < 60 * 60000 ? 5 * 60000 : 15 * 60000,
    lastAttempt = Date.parse(p.lastSellAttemptAt || 0);
  return !lastAttempt || Date.now() - lastAttempt >= retryMs;
}
function probeBlockers() {
  const b = [],
    active = state.probePositions.filter((p) => !probeIsQuarantined(p)),
    quarantined = state.probePositions.filter(probeIsQuarantined);
  if (!cfg.enabled) b.push("executor disabled");
  if (!cfg.probeLive) b.push("runner live-probe acknowledgement not armed");
  if (!cfg.discovery) b.push("discovery URL missing");
  if (!cfg.jupiter) b.push("Jupiter key missing");
  if (!cfg.helius) b.push("Helius key missing");
  if (!walletMatches()) b.push("signer wallet identity not verified");
  if (!state.balances?.checkedAt) b.push("wallet balances not verified");
  else if (Date.now() - Date.parse(state.balances.checkedAt) > 120000)
    b.push("wallet balances stale");
  if (num(state.balances?.usdc) < cfg.probeEntry)
    b.push("USDC balance below runner live-probe amount");
  if (num(state.balances?.sol) < 0.005)
    b.push("SOL balance below fee minimum");
  if (active.length >= cfg.probeMaxOpen)
    b.push(`runner live-probe open-position limit ${cfg.probeMaxOpen} reached`);
  if (quarantined.length >= cfg.probeMaxQuarantined)
    b.push(`runner live-probe quarantine limit ${cfg.probeMaxQuarantined} reached`);
  if (probeOpenExposureUsd() + cfg.probeEntry > cfg.probeMaxOpenExposureUsd)
    b.push("runner live-probe total open-exposure cap reached");
  if (probeDailyRealizedLossUsd() >= cfg.probeDailyCap)
    b.push("runner live-probe daily realized-loss cap reached");
  return b;
}
function emailBlockers() {
  const b = [];
  if (!cfg.emailEnabled) b.push("email disabled");
  if (!cfg.recipients.length) b.push("recipients missing");
  if (!cfg.from) b.push("from address missing");
  if (!cfg.client) b.push("Gmail client ID missing");
  if (!cfg.clientSecret) b.push("Gmail client secret missing");
  if (!cfg.refresh) b.push("Gmail refresh token missing");
  return b;
}
function publicState() {
  const address = walletAddress();
  return {
    ...state,
    seen: undefined,
    liveShadowSeen: undefined,
    walletAddress: address,
    walletSuffix: address.slice(-6),
    expectedWalletAddress: cfg.expectedAddress,
    walletIdentityVerified: walletMatches(),
    network: "mainnet-beta",
    usdcMint: USDC,
    live: cfg.live,
    ready: !blockers().length,
    blockers: blockers(),
    runnerLiveProbe: {
      enabled: cfg.probeLive,
      ready: !probeBlockers().length,
      blockers: probeBlockers(),
      entryUsd: cfg.probeEntry,
      dailyCapUsd: cfg.probeDailyCap,
      dailyCapType: "REALIZED_LOSS_ONLY",
      dailyRiskUsedUsd: probeDailyRealizedLossUsd(),
      openExposureUsd: probeOpenExposureUsd(),
      maxOpenExposureUsd: cfg.probeMaxOpenExposureUsd,
      spentTodayUsd: probeDailySpendUsd(),
      partialExitFraction: cfg.probePartialFraction,
      maxOpenPositions: cfg.probeMaxOpen,
      maxQuarantinedPositions: cfg.probeMaxQuarantined,
      quarantined: state.probePositions.filter(probeIsQuarantined).length,
      maxHoldMinutes: cfg.probeMaxHoldMinutes,
      open: state.probePositions.length,
      fills: state.probeFills.slice(0, 20),
    },
    paperPromotion: liveShadowStats(),
    liveShadowPromotion: liveShadowStats(),
    explorationPaperStats: paperStats(),
    strategyPerformance: strategyPerformance(),
    discoveryError: state.discoveryError || "",
    email: {
      ...state.email,
      enabled: cfg.emailEnabled,
      recipientCount: cfg.recipients.length,
      blockers: emailBlockers(),
    },
    limits: {
      live: { entryUsd: cfg.entry, totalUsd: cfg.total, maxPositions: cfg.max },
      paper: {
        entryUsd: cfg.paperEntry,
        totalUsd: cfg.paperTotal,
        maxPositions: cfg.paperMax,
        maxHoldMinutes: cfg.paperMaxHoldMinutes,
        costStressBps: cfg.paperCostStressBps,
      },
      stopPct: cfg.stop,
      targetPct: cfg.target,
      trailPct: cfg.trail,
    },
  };
}
async function json(url, opt = {}) {
  const r = await fetch(url, opt);
  if (!r.ok) throw Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function rpc(method, params) {
  const payload = await json(
    `https://mainnet.helius-rpc.com/?api-key=${encodeURIComponent(cfg.helius)}`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    },
  );
  if (payload?.error)
    throw Error(
      `Solana RPC ${method} failed: ${payload.error.message || JSON.stringify(payload.error)}`,
    );
  if (!Object.prototype.hasOwnProperty.call(payload || {}, "result"))
    throw Error(`Solana RPC ${method} returned no result`);
  return payload.result;
}
async function balances() {
  if (!walletMatches())
    throw Error(
      `refusing balance check for unverified signer ${walletAddress() || "missing"}`,
    );
  const owner = walletAddress(),
    [sol, tokens] = await Promise.all([
      rpc("getBalance", [owner]),
      rpc("getTokenAccountsByOwner", [
        owner,
        { mint: USDC },
        { encoding: "jsonParsed" },
      ]),
    ]);
  if (!Number.isFinite(Number(sol?.value)))
    throw Error("Solana RPC getBalance returned an invalid value");
  if (!Array.isArray(tokens?.value))
    throw Error("Solana RPC token-account response was invalid");
  state.balances = {
    sol: Number(sol.value) / 1e9,
    usdc: tokens.value.reduce(
      (n, x) => n + num(x.account?.data?.parsed?.info?.tokenAmount?.uiAmount),
      0,
    ),
    checkedAt: new Date().toISOString(),
    owner,
    network: "mainnet-beta",
    usdcMint: USDC,
  };
  state.balanceError = "";
  return state.balances;
}
let lastJupiterRequest = 0;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function order(inputMint, outputMint, amount) {
  const q = new URLSearchParams({
    inputMint,
    outputMint,
    amount: String(amount),
    taker: wallet.publicKey.toString(),
  });
  let lastError;
  for (let attempt = 0; attempt < 3; attempt++) {
    const spacing = 1000 - (Date.now() - lastJupiterRequest);
    if (spacing > 0) await wait(spacing);
    lastJupiterRequest = Date.now();
    try {
      return await json(`https://api.jup.ag/swap/v2/order?${q}`, {
        headers: { "x-api-key": cfg.jupiter },
      });
    } catch (e) {
      lastError = e;
      if (!String(e.message).startsWith("429 ") || attempt === 2) throw e;
      await wait(2000 * (attempt + 1));
    }
  }
  throw lastError;
}
async function execute(o) {
  if (!o.transaction) throw Error("Jupiter order has no transaction");
  const tx = VersionedTransaction.deserialize(
    Buffer.from(o.transaction, "base64"),
  );
  tx.sign([wallet]);
  return json("https://api.jup.ag/swap/v2/execute", {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": cfg.jupiter },
    body: JSON.stringify({
      signedTransaction: Buffer.from(tx.serialize()).toString("base64"),
      requestId: o.requestId,
      lastValidBlockHeight: o.lastValidBlockHeight,
    }),
  });
}
async function paperBuy(c) {
  const o = await order(USDC, c.mint, Math.round(cfg.paperEntry * 1e6)),
    qty = num(o.outAmount || o.outputAmount);
  if (!qty) throw Error(`no paper buy route for ${c.mint}`);
  const at = new Date().toISOString(),
    strategy = c.strategy || "SOLANA_EARLY_CONTROL",
    strategyVersion =
      c.strategy_version ||
      (strategy === "SOLANA_PUMPFUN_EV_EXPERIMENT"
        ? "DIVINE_V3"
        : strategy === "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
          ? "MICROCAP_LAUNCH_V2"
          : strategy === "SOLANA_MICROCAP_RUNNER_CAPTURE"
            ? "RUNNER_CAPTURE_V1"
            : "CONTROL_V2"),
    entryReason =
      c.entry_reason ||
      `Confirmed paper entry passed ${strategy} qualification gates with score ${num(c.score).toFixed(2)}.`,
    id = `paper:${strategy}:${c.mint}:${Date.now()}`,
    evidence = {
      confirmationScans: cfg.paperConfirmationScans,
      score: c.score,
      volume24hUsd: c.volume_24h_usd,
      liquidityUsd: c.liquidity_usd,
      uniqueBuyers5m: c.unique_buyers_5m,
      trades5m: c.trades_5m,
      netBuyPressure: c.net_buy_pressure,
      buyerAcceleration: c.buyer_acceleration,
      volumeAcceleration: c.volume_acceleration,
      priceChange5mPct: c.price_change_5m_pct,
      priceChange15mPct: c.price_change_15m_pct,
      returnSinceSeen: c.return_since_seen,
      retracementFromHigh: c.retracement_from_high,
      firstSeenAt: c.first_seen_at,
      sellPriceImpactBps: c.sell_price_impact_bps,
      top10HolderFraction: c.top10_holder_fraction,
      creatorFraction: c.creator_fraction,
      safetyEvidenceStatus: c.safety_evidence_status,
      distributionEvidenceStatus: c.distribution_evidence_status,
      walletEvidence: c.qualified_wallet_count || 0,
      flowDataProvenance: c.flow_data_provenance,
    };
  state.paperPositions.push({
    id,
    mint: c.mint,
    symbol: c.symbol || c.mint.slice(0, 6),
    strategy,
    strategyVersion,
    entryReason,
    entryEvidence: evidence,
    quantity: qty,
    entryUsd: cfg.paperEntry,
    highUsd: cfg.paperEntry,
    openedAt: at,
    score: c.score,
    evRank: c.ev_rank,
    probabilityProxy: c.probability_proxy,
    targetReturn: c.executable_win_return,
    walletEvidence: c.qualified_wallet_count || 0,
    downTicks: 0,
  });
  state.paperFills.unshift({
    id,
    action: "BUY",
    at,
    mint: c.mint,
    symbol: c.symbol,
    strategy,
    strategyVersion,
    entryReason,
    entryEvidence: evidence,
    entryUsd: cfg.paperEntry,
    score: c.score,
    evRank: c.ev_rank,
    targetReturn: c.executable_win_return,
    walletEvidence: c.qualified_wallet_count || 0,
  });
  state.lastPaperEntryAt[strategy] = at;
  return true;
}
async function paperClose(p, reason, o) {
  const proceeds = num(o.outAmount || o.outputAmount) / 1e6;
  if (!proceeds) return false;
  const at = new Date().toISOString(),
    pnl = proceeds - p.entryUsd,
    costStress = (p.entryUsd * cfg.paperCostStressBps) / 10000,
    adjusted = pnl - costStress,
    fill = {
      id: `close:${p.id}`,
      action: "SELL",
      at,
      mint: p.mint,
      symbol: p.symbol,
      strategy: p.strategy || "SOLANA_EARLY_CONTROL",
      strategyVersion: p.strategyVersion || "LEGACY_V1",
      entryReason:
        p.entryReason || "Historical paper entry reason was not stored.",
      entryEvidence: p.entryEvidence,
      reason,
      entryUsd: p.entryUsd,
      exitUsd: proceeds,
      highUsd: p.highUsd,
      maximumFavorablePnlUsd: num(p.highUsd, p.entryUsd) - p.entryUsd,
      holdSeconds: Math.max(
        0,
        (Date.parse(at) - Date.parse(p.openedAt)) / 1000,
      ),
      proceedsUsd: proceeds,
      realizedPnlUsd: pnl,
      costStressedPnlUsd: adjusted,
      returnPct: pnl / p.entryUsd,
    };
  state.paperFills.unshift(fill);
  state.postExitFollowups.unshift({
    id: fill.id,
    mint: p.mint,
    symbol: p.symbol,
    strategy: fill.strategy,
    strategyVersion: fill.strategyVersion,
    entryUsd: p.entryUsd,
    exitUsd: proceeds,
    quantity: p.quantity,
    closedAt: at,
    checkpoints: {},
  });
  state.postExitFollowups = state.postExitFollowups.slice(0, 40);
  state.paperPositions = state.paperPositions.filter((x) => x.id !== p.id);
  state.paperRealizedPnlUsd = num(state.paperRealizedPnlUsd) + pnl;
  state.paperObservations++;
  return true;
}
function paperVoid(p, detail) {
  state.paperFills.unshift({
    id: `void:${p.id}`,
    action: "VOID",
    at: new Date().toISOString(),
    mint: p.mint,
    symbol: p.symbol,
    strategy: p.strategy || "SOLANA_EARLY_CONTROL",
    reason: "UNPRICED_AT_MAX_HOLD",
    detail,
  });
  state.paperPositions = state.paperPositions.filter((x) => x.id !== p.id);
  state.paperObservations++;
  return true;
}
async function supervisePaper() {
  let changed = false;
  for (const p of [...state.paperPositions])
    try {
      const isPump = p.strategy === "SOLANA_PUMPFUN_EV_EXPERIMENT",
        isDivineV2 = p.strategyVersion === "DIVINE_V2",
        isDivineV3 = p.strategyVersion === "DIVINE_V3",
        isControlV2 = p.strategyVersion === "CONTROL_V2",
        isMicrocap = String(p.strategyVersion || "").startsWith(
          "MICROCAP_LAUNCH_",
        ),
        isRunner = p.strategyVersion === "RUNNER_CAPTURE_V1",
        ageMs = Date.now() - Date.parse(p.openedAt),
        maxHoldMs = isRunner
          ? 30 * 60000
          : isMicrocap
            ? 20 * 60000
            : isDivineV3
              ? 90 * 60000
              : isPump
                ? 3600e3
                : isControlV2
                  ? 120 * 60000
                  : cfg.paperMaxHoldMinutes * 60000,
        o = await order(p.mint, USDC, Math.floor(p.quantity)),
        mark = num(o.outAmount || o.outputAmount) / 1e6;
      if (!mark) {
        if (ageMs >= maxHoldMs)
          changed =
            paperVoid(p, "Jupiter returned no executable exit quote") ||
            changed;
        continue;
      }
      const prior = num(p.markUsd, p.entryUsd);
      p.downTicks = mark < prior * 0.995 ? num(p.downTicks) + 1 : 0;
      p.markUsd = mark;
      p.markedAt = new Date().toISOString();
      delete p.markError;
      p.highUsd = Math.max(num(p.highUsd, p.entryUsd), mark);
      const r = mark / p.entryUsd - 1,
        stop = isRunner
          ? 0.1
          : isMicrocap
            ? 0.08
            : isDivineV3
              ? 0.2
              : isPump
                ? 0.5
                : isControlV2
                  ? 0.15
                  : cfg.stop,
        target = isRunner
          ? 5.0
          : isMicrocap
            ? 0.2
            : isDivineV3
              ? 0.12
              : isDivineV2
                ? 0.1
                : isPump
                  ? Math.max(0.15, num(p.targetReturn, 0.4))
                  : isControlV2
                    ? 0.3
                    : cfg.target,
        failedMomentum =
          (isDivineV3 || isControlV2) &&
          ageMs >= 15 * 60000 &&
          r <= -0.08 &&
          p.highUsd <= p.entryUsd * 1.02,
        stalled =
          (isDivineV3 || isControlV2) &&
          ageMs >= 30 * 60000 &&
          r <= -0.03 &&
          p.highUsd < p.entryUsd * 1.05,
        divineTrail =
          (isDivineV2 || isDivineV3) &&
          p.highUsd >= p.entryUsd * 1.06 &&
          mark <= Math.max(p.entryUsd * 1.01, p.highUsd * 0.94),
        controlTrail =
          isControlV2 &&
          p.highUsd >= p.entryUsd * 1.1 &&
          mark <= Math.max(p.entryUsd * 1.02, p.highUsd * 0.9),
        microcapRollover =
          isMicrocap &&
          ageMs >= 3 * 60000 &&
          num(p.downTicks) >= 2 &&
          mark <= p.highUsd * 0.97,
        microcapProfitTrail =
          isMicrocap &&
          p.highUsd >= p.entryUsd * 1.06 &&
          mark <= Math.max(p.entryUsd * 1.01, p.highUsd * 0.96),
        runnerPeakReturn = p.highUsd / p.entryUsd - 1,
        runnerTrailPct =
          runnerPeakReturn >= 1
            ? 0.15
            : runnerPeakReturn >= 0.5
              ? 0.12
              : runnerPeakReturn >= 0.2
                ? 0.1
                : 0.08,
        runnerProfitFloor =
          runnerPeakReturn >= 1
            ? 0.6
            : runnerPeakReturn >= 0.5
              ? 0.25
              : runnerPeakReturn >= 0.2
                ? 0.08
                : 0.02,
        runnerTieredProfit =
          isRunner &&
          runnerPeakReturn >= 0.1 &&
          mark <= p.highUsd * (1 - runnerTrailPct) &&
          mark >= p.entryUsd * (1 + runnerProfitFloor),
        runnerRollover =
          isRunner &&
          ageMs >= 2 * 60000 &&
          num(p.downTicks) >= 2 &&
          mark <= p.highUsd * 0.95,
        reason =
          r <= -stop
            ? "STOP_LOSS"
            : r >= target
              ? "TAKE_PROFIT"
              : runnerTieredProfit
                ? "RUNNER_TIERED_PROFIT"
                : runnerRollover
                  ? "RUNNER_DOWNTREND"
                  : microcapProfitTrail
                    ? "MICROCAP_PROFIT_PROTECTION"
                    : microcapRollover
                      ? "MICROCAP_DOWNTREND"
                      : failedMomentum
                        ? "FAILED_MOMENTUM_15M"
                        : stalled
                          ? "STALLED_30M"
                          : divineTrail
                            ? "TRAILING_PROFIT"
                            : controlTrail
                              ? "TRAILING_PROFIT"
                              : !isPump &&
                                  !isControlV2 &&
                                  !isMicrocap &&
                                  !isRunner &&
                                  mark <= p.highUsd * (1 - cfg.trail) &&
                                  p.highUsd > p.entryUsd
                                ? "TRAILING_STOP"
                                : ageMs >= maxHoldMs
                                  ? isRunner
                                    ? "MAX_HOLD_30M"
                                    : isMicrocap
                                      ? "MAX_HOLD_20M"
                                      : isDivineV3
                                        ? "MAX_HOLD_90M"
                                        : isPump
                                          ? "MAX_HOLD_1H"
                                          : isControlV2
                                            ? "MAX_HOLD_120M"
                                            : `MAX_HOLD_${cfg.paperMaxHoldMinutes}M`
                                  : "";
      if (reason) changed = (await paperClose(p, reason, o)) || changed;
    } catch (e) {
      p.markError = e.message.slice(0, 180);
      const maxHoldMs =
        p.strategyVersion === "RUNNER_CAPTURE_V1"
          ? 30 * 60000
          : String(p.strategyVersion || "").startsWith("MICROCAP_LAUNCH_")
            ? 20 * 60000
            : p.strategyVersion === "DIVINE_V3"
              ? 90 * 60000
              : p.strategy === "SOLANA_PUMPFUN_EV_EXPERIMENT"
                ? 3600e3
                : p.strategyVersion === "CONTROL_V2"
                  ? 120 * 60000
                  : cfg.paperMaxHoldMinutes * 60000;
      if (Date.now() - Date.parse(p.openedAt) >= maxHoldMs)
        changed = paperVoid(p, p.markError) || changed;
    }
  return changed;
}
async function supervisePostExitFollowups() {
  const checkpoints = [15, 30, 60, 120, 240];
  for (const f of state.postExitFollowups || []) {
    const ageMinutes = (Date.now() - Date.parse(f.closedAt)) / 60000,
      due = checkpoints.find(
        (m) => ageMinutes >= m && !f.checkpoints[String(m)],
      );
    if (!due) continue;
    try {
      const o = await order(f.mint, USDC, Math.floor(f.quantity)),
        proceeds = num(o.outAmount || o.outputAmount) / 1e6;
      if (proceeds)
        f.checkpoints[String(due)] = {
          minutes: due,
          observedAt: new Date().toISOString(),
          executableProceedsUsd: proceeds,
          pnlVsEntryUsd: proceeds - f.entryUsd,
          deltaVsActualExitUsd: proceeds - f.exitUsd,
          wouldHaveImproved: proceeds > f.exitUsd,
        };
    } catch (e) {
      f.checkpoints[String(due)] = {
        minutes: due,
        observedAt: new Date().toISOString(),
        error: e.message.slice(0, 180),
      };
    }
  }
  state.postExitFollowups = (state.postExitFollowups || []).filter(
    (f) => Date.now() - Date.parse(f.closedAt) < 300 * 60000,
  );
}
async function liveShadowBuy(c) {
  const o = await order(USDC, c.mint, Math.round(cfg.entry * 1e6)),
    qty = num(o.outAmount || o.outputAmount);
  if (!qty) throw Error(`no live-strategy shadow buy route for ${c.mint}`);
  const at = new Date().toISOString(),
    id = `live-shadow:${c.mint}:${Date.now()}`;
  state.liveShadowPositions.push({
    id,
    mint: c.mint,
    symbol: c.symbol || c.mint.slice(0, 6),
    quantity: qty,
    entryUsd: cfg.entry,
    highUsd: cfg.entry,
    openedAt: at,
    score: c.score,
  });
  state.liveShadowFills.unshift({
    id,
    action: "BUY",
    at,
    mint: c.mint,
    symbol: c.symbol,
    entryUsd: cfg.entry,
    score: c.score,
  });
  return true;
}
async function liveShadowClose(p, reason, o) {
  const proceeds = num(o.outAmount || o.outputAmount) / 1e6;
  if (!proceeds) return false;
  const pnl = proceeds - p.entryUsd,
    costStress = (p.entryUsd * cfg.paperCostStressBps) / 10000;
  state.liveShadowFills.unshift({
    id: `close:${p.id}`,
    action: "SELL",
    at: new Date().toISOString(),
    mint: p.mint,
    symbol: p.symbol,
    reason,
    proceedsUsd: proceeds,
    realizedPnlUsd: pnl,
    costStressedPnlUsd: pnl - costStress,
    returnPct: pnl / p.entryUsd,
  });
  state.liveShadowPositions = state.liveShadowPositions.filter(
    (x) => x.id !== p.id,
  );
  state.liveShadowRealizedPnlUsd = num(state.liveShadowRealizedPnlUsd) + pnl;
  return true;
}
function liveShadowUnpricedClose(p, detail) {
  const pnl = -p.entryUsd,
    costStress = (p.entryUsd * cfg.paperCostStressBps) / 10000;
  state.liveShadowFills.unshift({
    id: `close:${p.id}`,
    action: "SELL",
    at: new Date().toISOString(),
    mint: p.mint,
    symbol: p.symbol,
    reason: "UNSELLABLE_AT_MAX_HOLD",
    detail,
    proceedsUsd: 0,
    realizedPnlUsd: pnl,
    costStressedPnlUsd: pnl - costStress,
    returnPct: -1,
  });
  state.liveShadowPositions = state.liveShadowPositions.filter(
    (x) => x.id !== p.id,
  );
  state.liveShadowRealizedPnlUsd = num(state.liveShadowRealizedPnlUsd) + pnl;
  return true;
}
async function superviseLiveShadow() {
  let changed = false;
  for (const p of [...state.liveShadowPositions])
    try {
      const o = await order(p.mint, USDC, Math.floor(p.quantity)),
        mark = num(o.outAmount || o.outputAmount) / 1e6;
      if (!mark) {
        if (Date.now() - Date.parse(p.openedAt) >= 864e5)
          changed =
            liveShadowUnpricedClose(
              p,
              "Jupiter returned no executable exit quote",
            ) || changed;
        continue;
      }
      p.markUsd = mark;
      p.markedAt = new Date().toISOString();
      delete p.markError;
      p.highUsd = Math.max(num(p.highUsd, p.entryUsd), mark);
      const r = mark / p.entryUsd - 1,
        reason =
          r <= -cfg.stop
            ? "STOP_LOSS"
            : r >= cfg.target
              ? "TAKE_PROFIT"
              : mark <= p.highUsd * (1 - cfg.trail) && p.highUsd > p.entryUsd
                ? "TRAILING_STOP"
                : Date.now() - Date.parse(p.openedAt) >= 864e5
                  ? "MAX_HOLD_24H"
                  : "";
      if (reason) changed = (await liveShadowClose(p, reason, o)) || changed;
    } catch (e) {
      p.markError = e.message.slice(0, 180);
      if (Date.now() - Date.parse(p.openedAt) >= 864e5)
        changed = liveShadowUnpricedClose(p, p.markError) || changed;
    }
  return changed;
}
async function recordProbeSell(p, quantity, action, reason) {
  const requested = Math.max(1, Math.floor(quantity));
  try {
    const o = await order(p.mint, USDC, requested),
      r = await execute(o);
    if (r.status !== "Success" || num(r.code) !== 0)
      throw Error(r.error || String(r.code));
    const proceeds = num(r.totalOutputAmount) / 1e6,
      at = new Date().toISOString();
    p.quantity = Math.max(0, p.quantity - requested);
    p.proceedsUsd = num(p.proceedsUsd) + proceeds;
    p.lastSellAt = at;
    p.lastSellSignature = r.signature;
    state.probeFills.unshift({
      id: r.signature,
      action,
      reason,
      at,
      mint: p.mint,
      symbol: p.symbol,
      quantity: requested,
      outputUsd: proceeds,
      originalInputUsd: p.entryUsd,
      cumulativeProceedsUsd: p.proceedsUsd,
      realizedPnlUsd: p.quantity <= 0 ? p.proceedsUsd - p.entryUsd : null,
      strategy: "SOLANA_MICROCAP_RUNNER_LIVE_PROBE",
    });
    if (p.quantity <= 0)
      state.probePositions = state.probePositions.filter(
        (x) => x.entrySignature !== p.entrySignature,
      );
    return true;
  } catch (e) {
    const at = new Date().toISOString();
    p.sellFailures = num(p.sellFailures) + 1;
    p.lastSellError = e.message.slice(0, 250);
    p.lastSellAttemptAt = at;
    state.probeFills.unshift({
      id: `probe-sell-failed:${p.mint}:${Date.now()}`,
      action: "PROBE_SELL_FAILED",
      reason,
      at,
      mint: p.mint,
      symbol: p.symbol,
      quantity: requested,
      error: p.lastSellError,
      strategy: "SOLANA_MICROCAP_RUNNER_LIVE_PROBE",
    });
    // Keep every retry in the audit ledger. After the initial escalation
    // milestones, send at most one reminder per hour.
    const priorAlertAt = Date.parse(
        p.lastFailureAlertAt || state.email.lastSentAt || 0,
      ),
      shouldNotify =
      p.sellFailures === 1 ||
      p.sellFailures === 5 ||
      p.sellFailures === 10 ||
      !priorAlertAt ||
      Date.now() - priorAlertAt >= 3600000;
    if (shouldNotify) p.lastFailureAlertAt = at;
    return shouldNotify;
  }
}
async function runnerProbeBuy(c) {
  const o = await order(USDC, c.mint, Math.round(cfg.probeEntry * 1e6)),
    r = await execute(o);
  if (r.status !== "Success" || num(r.code) !== 0)
    throw Error(`runner probe buy failed: ${r.error || r.code}`);
  const quantity = num(r.totalOutputAmount),
    at = new Date().toISOString(),
    p = {
      mint: c.mint,
      symbol: c.symbol || c.mint.slice(0, 6),
      quantity,
      originalQuantity: quantity,
      entryUsd: cfg.probeEntry,
      decimals: num(c.decimals, 6),
      proceedsUsd: 0,
      highTotalUsd: cfg.probeEntry,
      openedAt: at,
      entrySignature: r.signature,
      sellFailures: 0,
      entryEvidence: {
        score: c.score,
        volume24hUsd: c.volume_24h_usd,
        liquidityUsd: c.liquidity_usd,
        trades5m: c.trades_5m,
        uniqueBuyers5m: c.unique_buyers_5m,
        netBuyPressure: c.net_buy_pressure,
        priceChange5mPct: c.price_change_5m_pct,
        priceChange15mPct: c.price_change_15m_pct,
        returnSinceSeen: c.return_since_seen,
        retracementFromHigh: c.retracement_from_high,
        sellPriceImpactBps: c.sell_price_impact_bps,
        safetyEvidenceStatus: c.safety_evidence_status,
        poolAddress: c.pool_address,
        sourceObservedAt: c.source_observed_at,
        sourceUrl: c.source_url,
      },
    };
  state.probePositions.push(p);
  state.probeSeen[c.mint] = at;
  state.probeFills.unshift({
    id: r.signature,
    action: "PROBE_BUY",
    at,
    mint: c.mint,
    symbol: p.symbol,
    inputUsd: cfg.probeEntry,
    outputUnits: quantity,
    entryEvidence: p.entryEvidence,
    strategy: "SOLANA_MICROCAP_RUNNER_LIVE_PROBE",
  });
  const partialQuantity = Math.max(
    1,
    Math.floor(quantity * cfg.probePartialFraction),
  );
  await recordProbeSell(
    p,
    partialQuantity,
    "PROBE_PARTIAL_SELL",
    "IMMEDIATE_EXITABILITY_TEST",
  );
  return true;
}
async function superviseRunnerProbes() {
  let changed = false;
  for (const p of [...state.probePositions]) {
    // Keep background recovery active without hammering a dead route every
    // minute: five-minute retries initially, then fifteen-minute retries.
    if (!probeRetryDue(p)) continue;
    try {
      const o = await order(p.mint, USDC, Math.floor(p.quantity)),
        mark = num(o.outAmount || o.outputAmount) / 1e6,
        total = num(p.proceedsUsd) + mark;
      if (!mark) throw Error("runner probe sell quote returned no output");
      p.lastMarkedAt = new Date().toISOString();
      p.lastMarkUsd = mark;
      p.highTotalUsd = Math.max(num(p.highTotalUsd, p.entryUsd), total);
      if (!p.profitPartialSoldAt && total >= p.entryUsd * 1.08) {
        const quantityBefore = p.quantity;
        changed =
          (await recordProbeSell(
            p,
            Math.min(
              p.quantity,
              Math.max(
                1,
                Math.floor(p.originalQuantity * cfg.probePartialFraction),
              ),
            ),
            "PROBE_PROFIT_PARTIAL_SELL",
            "PROBE_PROFIT_CAPTURE_8PCT",
          )) || changed;
        if (p.quantity < quantityBefore)
          p.profitPartialSoldAt = new Date().toISOString();
        continue;
      }
      const ret = total / p.entryUsd - 1,
        retracement = p.highTotalUsd > 0 ? 1 - total / p.highTotalUsd : 0,
        reason =
          num(p.sellFailures) >= 2
            ? "PROBE_EXIT_ROUTE_RECOVERED"
            : ret <= -0.2
            ? "PROBE_STOP_LOSS"
            : ret >= 1
              ? "PROBE_TAKE_PROFIT_100"
              : p.highTotalUsd >= p.entryUsd * 1.2 && retracement >= 0.15
                ? "PROBE_TRAILING_ROLLOVER"
                : Date.now() - Date.parse(p.openedAt) >=
                    cfg.probeMaxHoldMinutes * 60000
                  ? "PROBE_MAX_HOLD_20M"
                  : "";
      if (reason)
        changed =
          (await recordProbeSell(
            p,
            p.quantity,
            "PROBE_FINAL_SELL",
            reason,
          )) || changed;
    } catch (e) {
      p.lastSellError = e.message.slice(0, 250);
      p.lastSellAttemptAt = new Date().toISOString();
      p.sellFailures = num(p.sellFailures) + 1;
      if (
        Date.now() - Date.parse(p.openedAt) >=
        cfg.probeMaxHoldMinutes * 60000
      )
        changed =
          (await recordProbeSell(
            p,
            p.quantity,
            "PROBE_FINAL_SELL",
            "PROBE_MAX_HOLD_RETRY",
          )) || changed;
    }
  }
  return changed;
}
async function buy(c) {
  const o = await order(USDC, c.mint, Math.round(cfg.entry * 1e6)),
    r = await execute(o);
  if (r.status !== "Success" || num(r.code) !== 0)
    throw Error(`buy failed: ${r.error || r.code}`);
  const fill = {
    id: r.signature,
    action: "BUY",
    mint: c.mint,
    symbol: c.symbol,
    at: new Date().toISOString(),
    outputUnits: num(r.totalOutputAmount),
    score: c.score,
  };
  state.fills.unshift(fill);
  state.positions.push({
    mint: c.mint,
    symbol: c.symbol,
    quantity: fill.outputUnits,
    entryUsd: cfg.entry,
    highUsd: cfg.entry,
    openedAt: fill.at,
    entrySignature: r.signature,
  });
}
async function sell(p, reason, o) {
  const r = await execute(
    o || (await order(p.mint, USDC, Math.floor(p.quantity))),
  );
  if (r.status !== "Success" || num(r.code) !== 0)
    throw Error(`sell failed: ${r.error || r.code}`);
  const proceeds = num(r.totalOutputAmount) / 1e6;
  state.fills.unshift({
    id: r.signature,
    action: "SELL",
    reason,
    mint: p.mint,
    symbol: p.symbol,
    at: new Date().toISOString(),
    realizedPnlUsd: proceeds - p.entryUsd,
  });
  state.positions = state.positions.filter(
    (x) => x.entrySignature !== p.entrySignature,
  );
}
async function superviseLive() {
  for (const p of [...state.positions]) {
    const o = await order(p.mint, USDC, Math.floor(p.quantity)),
      mark = num(o.outAmount || o.outputAmount) / 1e6;
    if (!mark) continue;
    p.highUsd = Math.max(num(p.highUsd, p.entryUsd), mark);
    const r = mark / p.entryUsd - 1,
      reason =
        r <= -cfg.stop
          ? "STOP_LOSS"
          : r >= cfg.target
            ? "TAKE_PROFIT"
            : mark <= p.highUsd * (1 - cfg.trail) && p.highUsd > p.entryUsd
              ? "TRAILING_STOP"
              : Date.now() - Date.parse(p.openedAt) >= 864e5
                ? "MAX_HOLD_24H"
                : "";
    if (reason) await sell(p, reason, o);
  }
}
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const strategyName = (s) =>
  s === "SOLANA_PUMPFUN_EV_EXPERIMENT"
    ? "Divine Strategy (Pump.fun EV)"
    : s === "SOLANA_MICROCAP_RUNNER_CAPTURE"
      ? "Runner Capture Experiment"
      : s === "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        ? "Microcap Launch Momentum"
        : "Solana Early Control";
const strategyStyle = (s) =>
  s.strategy === "SOLANA_PUMPFUN_EV_EXPERIMENT"
    ? "background:#f3e8ff;color:#581c87"
    : s.strategy === "SOLANA_MICROCAP_RUNNER_CAPTURE"
      ? "background:#ecfeff;color:#155e75"
      : s.strategy === "SOLANA_MICROCAP_LAUNCH_MOMENTUM"
        ? "background:#fff7ed;color:#9a3412"
        : "background:#ecfdf5;color:#065f46";
function report() {
  const shadow = liveShadowStats(),
    performance = strategyPerformance(),
    style = (s) =>
      s.strategy === "SOLANA_PUMPFUN_EV_EXPERIMENT"
        ? "background:#f3e8ff;color:#581c87"
        : "background:#ecfdf5;color:#065f46",
    cards = Object.values(performance)
      .map(
        (s) =>
          `<tr style="${style(s)}"><td><b>${esc(s.displayName || s.strategy)}</b></td><td>${s.actions}</td><td>${s.opened}</td><td>${s.closed}</td><td>${s.open}</td><td>${s.winRatePct.toFixed(1)}%</td><td>$${s.rawPnlUsd.toFixed(4)}</td><td>$${s.costStressedPnlUsd.toFixed(4)}</td><td>$${s.expectancyUsd.toFixed(4)}</td></tr>`,
      )
      .join(""),
    rows = state.paperFills
      .slice(0, 20)
      .map(
        (f) =>
          `<tr style="${style(f)}"><td>${esc(f.at)}</td><td><b>${esc(strategyName(f.strategy))}</b></td><td>${esc(f.action)}</td><td>${esc(f.symbol || f.mint.slice(0, 6))}</td><td>${esc(f.reason || "ENTRY")}</td><td>${f.evRank == null ? "-" : num(f.evRank).toFixed(4)}</td><td>${f.realizedPnlUsd == null ? "-" : `$${num(f.realizedPnlUsd).toFixed(4)}`}</td><td>${f.costStressedPnlUsd == null ? "-" : `$${num(f.costStressedPnlUsd).toFixed(4)}`}</td></tr>`,
      )
      .join("");
  return `<div style="font-family:Arial,sans-serif;color:#0f172a"><div style="background:#312e81;color:white;border-radius:14px;padding:20px"><h2 style="margin:0 0 8px">Solana Strategy Action Report</h2><div style="font-size:20px;font-weight:bold">Divine Strategy (Pump.fun EV) + Solana Early Control</div></div><p><span style="display:inline-block;background:#f3e8ff;color:#6b21a8;border:1px solid #c084fc;border-radius:999px;padding:7px 12px;font-weight:bold">PURPLE • Divine Strategy</span> <span style="display:inline-block;background:#ecfdf5;color:#065f46;border:1px solid #34d399;border-radius:999px;padding:7px 12px;font-weight:bold">GREEN • Solana Early Control</span></p><p><b>PAPER ONLY - no funds were spent by either exploration strategy.</b></p><p>Generated ${new Date().toISOString()}. This report is emailed only when a BUY or SELL action occurs.</p><p><b>Strict live-strategy shadow:</b> Closed ${shadow.closed}/${cfg.minPaper}; cost-stressed P&amp;L $${shadow.costStressedPnlUsd.toFixed(4)}; open ${state.liveShadowPositions.length}.</p><table border="0" cellspacing="0" cellpadding="8" style="border-collapse:collapse;width:100%"><tr style="background:#e2e8f0"><th>Strategy</th><th>Actions</th><th>Buys</th><th>Closed</th><th>Open</th><th>Win rate</th><th>Raw P&amp;L</th><th>Cost-stressed P&amp;L</th><th>Expectancy</th></tr>${cards}</table><p>Wallet: ...${esc(wallet?.publicKey.toString().slice(-6) || "missing")}; Live blockers: ${esc(blockers().join("; ") || "none")}</p><h3>Recent actions</h3><table border="0" cellspacing="0" cellpadding="8" style="border-collapse:collapse;width:100%"><tr style="background:#e2e8f0"><th>UTC</th><th>Strategy</th><th>Action</th><th>Token</th><th>Reason</th><th>EV rank</th><th>Raw P&amp;L</th><th>Stressed P&amp;L</th></tr>${rows || "<tr><td colspan=8>No paper fills yet</td></tr>"}</table></div>`;
}
function actionExplanation(f) {
  if (f.action === "BUY")
    return (
      f.entryReason ||
      "Historical entry: detailed qualification evidence was not stored."
    );
  const labels = {
    STOP_LOSS: "Executable value reached the strategy hard-loss boundary.",
    TAKE_PROFIT: "Executable value reached the strategy profit target.",
    MICROCAP_DOWNTREND:
      "Two consecutive executable marks declined and price retraced at least 3% from the high, confirming rollover.",
    MICROCAP_PROFIT_PROTECTION:
      "The launch gained at least 6%, then retraced 4%; the paper exit protected at least 1% over entry.",
    RUNNER_TIERED_PROFIT:
      "The explosive runner retraced through its gain-dependent profit tier, so the paper experiment captured the remaining protected gain.",
    RUNNER_DOWNTREND:
      "Two consecutive executable values declined and the runner retraced at least 5% from its peak.",
    MAX_HOLD_20M: "The 20-minute launch-momentum observation window ended.",
    MAX_HOLD_30M: "The 30-minute runner-capture observation window ended.",
    FAILED_MOMENTUM_15M:
      "After 15 minutes the position remained down at least 8% and had never gained 2%.",
    STALLED_30M:
      "After 30 minutes the position remained down without first gaining 5%.",
    TRAILING_PROFIT:
      "A gain retraced from its high-water mark, so profit was protected.",
    MAX_HOLD_1H: "The legacy one-hour observation window ended.",
    MAX_HOLD_90M: "The Divine V3 90-minute observation window ended.",
    MAX_HOLD_120M: "The Control V2 120-minute observation window ended.",
    MAX_HOLD_60M: "The legacy maximum paper holding window ended.",
    TRAILING_STOP: "Price retraced through the trailing protection level.",
  };
  return labels[f.reason] || f.reason || "Paper position state changed.";
}
function reportV2() {
  const shadow = liveShadowStats(),
    performance = strategyPerformance(),
    divineV2 = strategyVersionStats(
      "SOLANA_PUMPFUN_EV_EXPERIMENT",
      "DIVINE_V2",
    ),
    lastSent = Date.parse(state.email.lastSentAt || 0),
    newFills = state.paperFills.filter((f) => Date.parse(f.at) > lastSent),
    newest = newFills[0],
    cards = Object.values(performance)
      .map(
        (s) =>
          `<tr style="${strategyStyle(s)}"><td><b>${esc(s.displayName || s.strategy)}</b></td><td>${s.actions}</td><td>${s.opened}</td><td>${s.closed}</td><td>${s.open}</td><td>${s.winRatePct.toFixed(1)}%</td><td>$${s.rawPnlUsd.toFixed(4)}</td><td>$${s.costStressedPnlUsd.toFixed(4)}</td><td>$${s.expectancyUsd.toFixed(4)}</td></tr>`,
      )
      .join(""),
    rows = state.paperFills
      .slice(0, 20)
      .map((f) => {
        const isNew = Date.parse(f.at) > lastSent,
          badge = isNew
            ? '<span style="background:#dc2626;color:white;padding:3px 7px;border-radius:999px;font-weight:bold">NEW</span> '
            : "",
          rowStyle = isNew
            ? "background:#fef3c7;color:#78350f;border:3px solid #f59e0b"
            : strategyStyle(f);
        return `<tr style="${rowStyle}"><td>${esc(f.at)}</td><td><b>${esc(strategyName(f.strategy))}</b></td><td>${badge}${esc(f.action)}</td><td>${esc(f.symbol || f.mint.slice(0, 6))}</td><td>${esc(actionExplanation(f))}</td><td>${f.evRank == null ? "-" : num(f.evRank).toFixed(4)}</td><td>${f.realizedPnlUsd == null ? "-" : `$${num(f.realizedPnlUsd).toFixed(4)}`}</td><td>${f.costStressedPnlUsd == null ? "-" : `$${num(f.costStressedPnlUsd).toFixed(4)}`}</td></tr>`;
      })
      .join("");
  return `<div style="font-family:Arial,sans-serif;color:#0f172a"><div style="background:#312e81;color:white;border-radius:14px;padding:20px"><span style="display:inline-block;background:#dc2626;color:white;padding:6px 11px;border-radius:999px;font-weight:900">NEW ACTION</span><h2 style="margin:10px 0 8px">Solana Strategy Action Report</h2><div style="font-size:20px;font-weight:bold">Divine Strategy + Solana Early Control + Microcap Launch V2 + Runner Capture V1</div></div>${newest ? `<div style="margin-top:12px;padding:16px;border:3px solid #f59e0b;background:#fef3c7;border-radius:8px"><b>REASON FOR ENTRY / ACTION</b><div style="margin-top:7px">${esc(actionExplanation(newest))}</div></div>` : ""}<p><span style="display:inline-block;background:#f3e8ff;color:#6b21a8;border:1px solid #c084fc;border-radius:999px;padding:7px 12px;font-weight:bold">PURPLE • Divine Strategy</span> <span style="display:inline-block;background:#ecfdf5;color:#065f46;border:1px solid #34d399;border-radius:999px;padding:7px 12px;font-weight:bold">GREEN • Solana Early Control</span> <span style="display:inline-block;background:#fff7ed;color:#9a3412;border:1px solid #fb923c;border-radius:999px;padding:7px 12px;font-weight:bold">ORANGE • Microcap Launch V2</span> <span style="display:inline-block;background:#ecfeff;color:#155e75;border:1px solid #22d3ee;border-radius:999px;padding:7px 12px;font-weight:bold">CYAN • Runner Capture V1</span></p><p><b>PAPER ONLY - no funds were spent.</b></p><p><b>Divine V2 forward sample:</b> ${divineV2.closed} closed, ${divineV2.open} open, ${divineV2.winRatePct.toFixed(1)}% win rate, $${divineV2.costStressedPnlUsd.toFixed(4)} cost-stressed P&amp;L. Historical V1 results remain in the all-time row below.</p><p><b>Strict live-strategy shadow:</b> Closed ${shadow.closed}/${cfg.minPaper}; cost-stressed P&amp;L $${shadow.costStressedPnlUsd.toFixed(4)}; open ${state.liveShadowPositions.length}.</p><table cellspacing="0" cellpadding="8" style="border-collapse:collapse;width:100%"><tr style="background:#e2e8f0"><th>Strategy</th><th>Actions</th><th>Buys</th><th>Closed</th><th>Open</th><th>Win rate</th><th>Raw P&amp;L</th><th>Cost-stressed P&amp;L</th><th>Expectancy</th></tr>${cards}</table><h3>Recent actions</h3><table cellspacing="0" cellpadding="8" style="border-collapse:collapse;width:100%"><tr style="background:#e2e8f0"><th>UTC</th><th>Strategy</th><th>Action</th><th>Token</th><th>Reason for entry / action</th><th>EV rank</th><th>Raw P&amp;L</th><th>Stressed P&amp;L</th></tr>${rows || "<tr><td colspan=8>No paper fills yet</td></tr>"}</table></div>`;
}
function reportV3() {
  const divine = strategyVersionStats(
      "SOLANA_PUMPFUN_EV_EXPERIMENT",
      "DIVINE_V3",
    ),
    control = strategyVersionStats("SOLANA_EARLY_CONTROL", "CONTROL_V2"),
    micro = strategyVersionStats(
      "SOLANA_MICROCAP_LAUNCH_MOMENTUM",
      "MICROCAP_LAUNCH_V2",
    ),
    runner = strategyVersionStats(
      "SOLANA_MICROCAP_RUNNER_CAPTURE",
      "RUNNER_CAPTURE_V1",
    ),
    positions =
      state.paperPositions
        .filter((p) =>
          [
            "DIVINE_V3",
            "CONTROL_V2",
            "MICROCAP_LAUNCH_V2",
            "RUNNER_CAPTURE_V1",
          ].includes(p.strategyVersion),
        )
        .map(
          (p) =>
            `<li><b>${esc(p.strategyVersion)} ${esc(p.symbol)}</b> • mint ${esc(p.mint)} • $${num(p.entryUsd).toFixed(2)} paper amount • score ${num(p.score).toFixed(2)} • ${esc(p.entryReason)}</li>`,
        )
        .join("") || "<li>No current-version positions open.</li>",
    summary = `<div style="margin:14px 0;padding:14px;border:2px solid #7c3aed;background:#faf5ff;border-radius:10px"><h3 style="margin-top:0">NEW evidence-confirmed strategy versions</h3><p><b>Divine V3:</b> ${divine.closed} closed, ${divine.open} open, ${divine.winRatePct.toFixed(1)}% wins, $${divine.costStressedPnlUsd.toFixed(4)} stressed P&amp;L. <b>Control V2:</b> ${control.closed} closed, ${control.open} open, ${control.winRatePct.toFixed(1)}% wins, $${control.costStressedPnlUsd.toFixed(4)} stressed P&amp;L. <b>Microcap Launch V2:</b> ${micro.closed} closed, ${micro.open} open, ${micro.winRatePct.toFixed(1)}% wins, $${micro.costStressedPnlUsd.toFixed(4)} stressed P&amp;L. <b>Runner Capture V1:</b> ${runner.closed} closed, ${runner.open} open, ${runner.winRatePct.toFixed(1)}% wins, $${runner.costStressedPnlUsd.toFixed(4)} stressed P&amp;L.</p><p><b>Microcap entry:</b> at least $100k rolling 24-hour volume, a pool no older than 30 minutes, serious five-minute momentum, persistent buyer/volume acceleration across two scans, verified safety/concentration, and an executable Jupiter sell route.</p><p><b>Runner Capture entry:</b> paper only; at least $100k volume, 20% retained gain, 15% current five-minute momentum, positive fifteen-minute momentum, two-scan persistence, executable sell route, adequate liquidity and no more than 10% retracement from the observed high.</p><p><b>Runner Capture exit:</b> 10% hard stop, gain-dependent 8–15% trailing protection, two-tick 5% rollover confirmation, 500% terminal target, or 30-minute maximum hold.</p><ul>${positions}</ul></div>`;
  const probeRows = state.probeFills
    .slice(0, 20)
    .map(
      (f) =>
        `<tr style="background:#fee2e2;color:#7f1d1d"><td>${esc(f.at)}</td><td><b>LIVE RUNNER PROBE</b></td><td>${esc(f.action)}</td><td>${esc(f.symbol || f.mint?.slice(0, 6))}</td><td>${esc(f.reason || "REAL_MONEY_ENTRY")}</td><td>${f.inputUsd == null ? "-" : `${num(f.inputUsd).toFixed(4)}`}</td><td>${f.outputUsd == null ? "-" : `${num(f.outputUsd).toFixed(4)}`}</td><td>${f.realizedPnlUsd == null ? "-" : `${num(f.realizedPnlUsd).toFixed(4)}`}</td><td>${esc(f.id)}</td><td>${esc(f.error || "")}</td></tr>`,
    )
    .join("");
  const probeSummary = `<div style="margin:16px 0;padding:16px;border:3px solid #dc2626;background:#fef2f2;border-radius:10px"><h3 style="margin-top:0">REAL-MONEY RUNNER LIQUIDITY PROBE</h3><p><b>Amount per probe:</b> ${cfg.probeEntry.toFixed(2)}; <b>daily realized-loss cap:</b> ${cfg.probeDailyCap.toFixed(2)}; <b>realized loss used:</b> ${probeDailyRealizedLossUsd().toFixed(4)}; <b>open exposure:</b> ${probeOpenExposureUsd().toFixed(4)} / $${cfg.probeMaxOpenExposureUsd.toFixed(2)}; <b>gross entries today:</b> ${probeDailySpendUsd().toFixed(2)}; <b>open:</b> ${state.probePositions.length}; <b>quarantined:</b> ${state.probePositions.filter(probeIsQuarantined).length}</p><p>Unsellable positions move to background quarantine after the maximum hold. They continue receiving exit attempts without occupying an active trading slot.</p><p><b>Status:</b> ${esc(probeBlockers().join("; ") || "ARMED AND READY")}</p><table cellspacing="0" cellpadding="7" style="border-collapse:collapse;width:100%"><tr><th>UTC</th><th>Strategy</th><th>Action</th><th>Token</th><th>Reason</th><th>Input</th><th>Recovered</th><th>P&amp;L</th><th>Transaction signature</th><th>Error</th></tr>${probeRows || '<tr><td colspan="10">No real probe action yet</td></tr>'}</table></div>`;
  return reportV2().replace(
    "<h3>Recent actions</h3>",
    summary + probeSummary + "<h3>Recent actions</h3>",
  );
}
function runnerProbeEmailReport() {
  const lastSent = Date.parse(state.email.lastSentAt || 0),
    newest =
      state.probeFills.find((f) => Date.parse(f.at) > lastSent) ||
      state.probeFills[0],
    buys = state.probeFills.filter((f) => f.action === "PROBE_BUY"),
    successfulSells = state.probeFills.filter(
      (f) => f.action.includes("SELL") && f.action !== "PROBE_SELL_FAILED",
    ),
    completed = state.probeFills.filter(
      (f) => f.action === "PROBE_FINAL_SELL" && f.realizedPnlUsd != null,
    ),
    grossEntries = buys.reduce((n, f) => n + num(f.inputUsd), 0),
    recovered = successfulSells.reduce((n, f) => n + num(f.outputUsd), 0),
    realizedBeforeFees = completed.reduce(
      (n, f) => n + num(f.realizedPnlUsd),
      0,
    ),
    open = state.probePositions.find((p) => p.mint === newest?.mint),
    actionLabel =
      newest?.action === "PROBE_SELL_FAILED"
        ? "EXIT RETRY FAILED — NO TRADE OCCURRED"
        : newest?.action === "PROBE_BUY"
          ? "BUY COMPLETED"
          : newest?.action === "PROBE_PARTIAL_SELL"
            ? "LIQUIDITY-TEST SALE COMPLETED"
            : newest?.action === "PROBE_PROFIT_PARTIAL_SELL"
              ? "PROFIT PARTIAL SALE COMPLETED"
              : newest?.action === "PROBE_FINAL_SELL"
                ? "POSITION CLOSED"
                : newest?.action || "STATUS UPDATE",
    reasonText =
      newest?.action === "PROBE_SELL_FAILED"
        ? "The system tried to sell the open token position, but Jupiter could not provide an executable route. No tokens moved and no new loss was realized by this retry."
        : newest?.reason === "IMMEDIATE_EXITABILITY_TEST"
          ? "The strategy sold a small portion immediately to verify that the token could be converted back to USDC."
          : newest?.reason === "PROBE_PROFIT_CAPTURE_8PCT"
            ? "Executable total value reached the +8% profit-capture level."
            : newest?.reason || "The live runner-probe position changed.",
    actionAmount =
      newest?.inputUsd != null
        ? `$${num(newest.inputUsd).toFixed(6)} spent`
        : newest?.outputUsd != null
          ? `$${num(newest.outputUsd).toFixed(6)} recovered`
          : newest?.quantity != null
            ? `${(
                num(newest.quantity) /
                10 ** num(open?.decimals, 6)
              ).toFixed(6)} ${newest.symbol || "tokens"} requested for sale; none sold`
            : "Not reported",
    status = open
      ? `${probeIsQuarantined(open) ? "QUARANTINED — background exit monitoring continues" : "OPEN"} — $${Math.max(0, num(open.entryUsd) - num(open.proceedsUsd)).toFixed(6)} unrecovered cost basis; ${num(open.sellFailures)} failed sell attempts`
      : "CLOSED",
    result = completed.length
      ? `$${realizedBeforeFees.toFixed(6)} realized before network fees`
      : "No completed round trip; final profit/loss is not yet known",
    recent = state.probeFills
      .filter((f) => f.action !== "PROBE_SELL_FAILED")
      .slice(0, 6)
      .map(
        (f) =>
          `<tr><td>${esc(f.at)}</td><td>${esc(f.symbol || f.mint?.slice(0, 6))}</td><td>${esc(f.action)}</td><td>${f.inputUsd != null ? `$${num(f.inputUsd).toFixed(6)}` : "-"}</td><td>${f.outputUsd != null ? `$${num(f.outputUsd).toFixed(6)}` : "-"}</td></tr>`,
      )
      .join("");
  return `<div style="font-family:Arial,sans-serif;color:#172033;max-width:760px;margin:auto"><div style="background:#0f3d56;color:white;padding:20px;border-radius:14px 14px 0 0"><span style="background:${newest?.action === "PROBE_SELL_FAILED" ? "#dc2626" : "#f97316"};color:white;border-radius:999px;padding:5px 10px;font-weight:800">${newest?.action === "PROBE_SELL_FAILED" ? "EXECUTION ALERT" : "NEW ACTION"}</span><h2 style="margin:12px 0 4px">Runner Probe — ${esc(newest?.symbol || "Unknown token")}</h2><div>${esc(actionLabel)}</div></div><div style="border:1px solid #cbd5e1;padding:18px;border-radius:0 0 14px 14px"><div style="background:#fff7ed;border-left:5px solid #f97316;padding:12px;margin-bottom:14px"><b>Why this report was sent</b><br>${esc(reasonText)}</div><table cellspacing="0" cellpadding="8" style="border-collapse:collapse;width:100%;background:#f8fafc"><tr><td><b>Action amount</b><br>${esc(actionAmount)}</td><td><b>Position status</b><br>${esc(status)}</td></tr><tr><td><b>Completed result</b><br>${esc(result)}</td><td><b>Risk controls</b><br>Realized loss today: $${probeDailyRealizedLossUsd().toFixed(6)} / $${cfg.probeDailyCap.toFixed(2)}<br>Open exposure: $${probeOpenExposureUsd().toFixed(6)} / $${cfg.probeMaxOpenExposureUsd.toFixed(2)}</td></tr></table>${newest?.error ? `<div style="margin-top:12px;padding:10px;background:#fee2e2;color:#991b1b"><b>Execution problem:</b> No executable Jupiter sell route is currently available. The position is quarantined after the maximum hold and remains under background exit supervision without occupying the active trading lane.</div>` : ""}<p><b>Service totals:</b> ${buys.length} buys; ${successfulSells.length} successful sales; ${completed.length} completed round trips; ${state.probePositions.length} open position${state.probePositions.length === 1 ? "" : "s"}; ${state.probePositions.filter(probeIsQuarantined).length} quarantined. Gross entries: $${grossEntries.toFixed(6)}. USDC recovered: $${recovered.toFixed(6)}. Network fees are not included unless explicitly recorded.</p><h3>Successful actions</h3><table cellspacing="0" cellpadding="7" style="border-collapse:collapse;width:100%"><tr style="background:#e2e8f0"><th>UTC</th><th>Token</th><th>Action</th><th>Spent</th><th>Recovered</th></tr>${recent || '<tr><td colspan="5">No successful action recorded</td></tr>'}</table><p style="color:#64748b;font-size:12px">Every failed retry remains in the audit ledger. After the first, fifth and tenth failures, reminder emails are limited to once per hour.</p></div></div>`;
}
async function email(hasTradeEvent = false) {
  if (hasTradeEvent)
    state.email = {
      ...state.email,
      pendingTradeEvent: true,
      pendingSince: state.email.pendingSince || new Date().toISOString(),
    };
  if (!state.email.pendingTradeEvent || emailBlockers().length) return false;
  const body = new URLSearchParams({
      client_id: cfg.client,
      client_secret: cfg.clientSecret,
      refresh_token: cfg.refresh,
      grant_type: "refresh_token",
    }),
    t = await json("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
  const shadow = liveShadowStats(),
    newCount =
      state.paperFills.filter(
        (f) => Date.parse(f.at) > Date.parse(state.email.lastSentAt || 0),
      ).length +
      state.probeFills.filter(
        (f) => Date.parse(f.at) > Date.parse(state.email.lastSentAt || 0),
      ).length,
    probeNew = state.probeFills.some(
      (f) => Date.parse(f.at) > Date.parse(state.email.lastSentAt || 0),
    ),
    subject = `[TRADE] Solana ${probeNew ? "LIVE PROBE" : "NEW"} ${newCount} action${newCount === 1 ? "" : "s"} | strict ${shadow.closed} closed | ${shadow.costStressedPnlUsd.toFixed(2)} P&L`,
    mime = [
      `From: ${cfg.from}`,
      `To: ${cfg.recipients.join(", ")}`,
      `Subject: ${subject}`,
      "MIME-Version: 1.0",
      "Content-Type: text/html; charset=UTF-8",
      "",
      probeNew ? runnerProbeEmailReport() : reportV3(),
    ].join("\r\n");
  await json("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {
    method: "POST",
    headers: {
      authorization: `Bearer ${t.access_token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ raw: Buffer.from(mime).toString("base64url") }),
  });
  state.email = {
    ...state.email,
    lastSentAt: new Date().toISOString(),
    lastError: "",
    sentCount: num(state.email.sentCount) + 1,
    mode: "TRADE_EVENTS_ONLY",
    pendingTradeEvent: false,
    pendingSince: "",
  };
  return true;
}
function confirmCandidate(c, strategy, scanAt) {
  const key = `${strategy}:${c.mint}`,
    prior = state.confirmations[key],
    scan = scanAt || new Date().toISOString(),
    consecutive =
      prior &&
      prior.lastScan !== scan &&
      Date.parse(scan) - Date.parse(prior.lastScan) <= 180000;
  state.confirmations[key] = {
    count: consecutive ? prior.count + 1 : 1,
    firstScan: consecutive ? prior.firstScan : scan,
    lastScan: scan,
    score: c.score,
    priceChange5mPct: c.price_change_5m_pct,
    netBuyPressure: c.net_buy_pressure,
  };
  return state.confirmations[key].count >= cfg.paperConfirmationScans;
}
async function tick() {
  if (!cfg.enabled) return;
  let data = { candidates: [] },
    candidates = [];
  if (cfg.discovery)
    try {
      data = await json(`${cfg.discovery}/candidates`);
      candidates = data.candidates || [];
      state.discoveryError = "";
      state.discoveryDiagnostics = data.strategy_diagnostics || {};
      state.microcapWatchlist = data.microcap_watchlist || [];
      state.microcapWatchlistSummary = data.microcap_watchlist_summary || {};
      state.watchedWallets = data.watched_wallets || [];
      state.walletEvidence = data.wallet_evidence || [];
      state.lastSuccessfulDiscoveryAt = new Date().toISOString();
    } catch (e) {
      state.discoveryError = e.message.slice(0, 500);
    }
  let changed = await supervisePaper();
  await supervisePostExitFollowups();
  changed = (await superviseLiveShadow()) || changed;
  changed = (await superviseRunnerProbes()) || changed;
  for (const c of candidates.filter((x) => x.paper_qualified === true)) {
    const strategy = c.strategy || "SOLANA_EARLY_CONTROL",
      seenKey = `${strategy}:${c.mint}`,
      scanAt = data.scanned_at || new Date().toISOString();
    if (state.seen[seenKey] || !confirmCandidate(c, strategy, scanAt)) continue;
    const strategyOpen = state.paperPositions.filter(
        (p) => (p.strategy || "SOLANA_EARLY_CONTROL") === strategy,
      ).length,
      lastEntry = Date.parse(state.lastPaperEntryAt[strategy] || 0),
      cooldownOk =
        !lastEntry ||
        Date.now() - lastEntry >= cfg.paperEntryCooldownSeconds * 1000;
    if (
      strategyOpen < cfg.paperMaxPerStrategy &&
      cooldownOk &&
      state.paperPositions.length < cfg.paperMax &&
      exposure(state.paperPositions) + cfg.paperEntry <= cfg.paperTotal
    ) {
      changed = (await paperBuy(c)) || changed;
      state.seen[seenKey] = scanAt;
    }
  }
  for (const c of candidates.filter((x) => x.qualified === true)) {
    if (state.liveShadowSeen[c.mint]) continue;
    state.liveShadowSeen[c.mint] = data.scanned_at || new Date().toISOString();
    if (
      state.liveShadowPositions.length < cfg.max &&
      exposure(state.liveShadowPositions) + cfg.entry <= cfg.total
    )
      changed = (await liveShadowBuy(c)) || changed;
  }
  if (wallet && cfg.helius)
    try {
      await balances();
    } catch (e) {
      state.balanceError = e.message.slice(0, 250);
    }
  if (!probeBlockers().length) {
    const scanAt = data.scanned_at || new Date().toISOString();
    for (const candidate of candidates.filter(
      (x) =>
        x.strategy === "SOLANA_MICROCAP_RUNNER_CAPTURE" &&
        x.live_probe_qualified === true,
    )) {
      if (
        state.probeSeen[candidate.mint] ||
        !confirmCandidate(candidate, "RUNNER_LIVE_PROBE", scanAt)
      )
        continue;
      changed = (await runnerProbeBuy(candidate)) || changed;
      break;
    }
  }
  if (!blockers().length) {
    await superviseLive();
    for (const c of candidates.filter((x) => x.qualified === true)) {
      if (
        state.positions.length >= cfg.max ||
        exposure(state.positions) + cfg.entry > cfg.total
      )
        break;
      if (!state.positions.some((p) => p.mint === c.mint)) await buy(c);
    }
  }
  try {
    await email(changed);
  } catch (e) {
    state.email = {
      ...state.email,
      lastError: e.message.slice(0, 250),
      lastAttemptAt: new Date().toISOString(),
    };
  }
  save();
}
const fail = (e) => {
  state.errors.unshift(`${new Date().toISOString()} ${e.message}`);
  state.errors = state.errors.slice(0, 20);
  save();
};
setInterval(
  () => tick().catch(fail),
  Math.max(60, num(env("SOLANA_EXECUTOR_INTERVAL_SECONDS"), 60)) * 1000,
);
http
  .createServer((req, res) => {
    if (!["/health", "/status", "/report", "/report.json"].includes(req.url)) {
      res.writeHead(404).end();
      return;
    }
    if (req.url === "/report") {
      res.writeHead(200, {
        "content-type": "text/html; charset=UTF-8",
        "cache-control": "no-store",
      });
      res.end(reportV3());
      return;
    }
    const p = publicState(),
      body = JSON.stringify(
        req.url === "/health"
          ? {
              service: "solana-executor",
              ...p,
              positions: undefined,
              fills: undefined,
              paperFills: undefined,
              errors: state.errors.slice(0, 3),
            }
          : req.url === "/report.json"
            ? {
                generatedAt: new Date().toISOString(),
                paperOnly: true,
                emailMode: "TRADE_EVENTS_ONLY",
                strategyPerformance: p.strategyPerformance,
                divineV2Performance: strategyVersionStats(
                  "SOLANA_PUMPFUN_EV_EXPERIMENT",
                  "DIVINE_V2",
                ),
                divineV3Performance: strategyVersionStats(
                  "SOLANA_PUMPFUN_EV_EXPERIMENT",
                  "DIVINE_V3",
                ),
                controlV2Performance: strategyVersionStats(
                  "SOLANA_EARLY_CONTROL",
                  "CONTROL_V2",
                ),
                microcapLaunchV2Performance: strategyVersionStats(
                  "SOLANA_MICROCAP_LAUNCH_MOMENTUM",
                  "MICROCAP_LAUNCH_V2",
                ),
                runnerCaptureV1Performance: strategyVersionStats(
                  "SOLANA_MICROCAP_RUNNER_CAPTURE",
                  "RUNNER_CAPTURE_V1",
                ),
                discoveryDiagnostics: p.discoveryDiagnostics,
                microcapWatchlist: (state.microcapWatchlist || []).slice(
                  0,
                  100,
                ),
                microcapWatchlistSummary: state.microcapWatchlistSummary || {},
                watchedWallets: state.watchedWallets || [],
                walletEvidence: state.walletEvidence || [],
                lastSuccessfulDiscoveryAt: p.lastSuccessfulDiscoveryAt,
                discoveryError: p.discoveryError,
                recentActions: state.paperFills.slice(0, 20),
                postExitCounterfactuals: (state.postExitFollowups || []).slice(
                  0,
                  20,
                ),
              }
            : p,
      );
    res.writeHead(200, {
      "content-type": "application/json",
      "cache-control": "no-store",
    });
    res.end(body);
  })
  .listen(num(env("PORT"), 8080));
tick().catch(fail);
