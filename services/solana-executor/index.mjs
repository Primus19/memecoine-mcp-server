import http from "node:http";
import fs from "node:fs";
import { Keypair, VersionedTransaction } from "@solana/web3.js";
import bs58 from "bs58";

const USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const DATA_PATH = process.env.SOLANA_EXECUTOR_STATE_PATH || "/app/data/solana_executor.json";
const LIVE_ACK = "I_ACCEPT_THE_25_USD_SOLANA_EARLY_RISK";
const cfg = {
  discovery: (process.env.SOLANA_DISCOVERY_URL || "").replace(/\/$/, ""),
  apiKey: process.env.JUPITER_API_KEY || "",
  heliusKey: process.env.HELIUS_API_KEY || "",
  secret: process.env.SOLANA_WALLET_PRIVATE_KEY || "",
  enabled: process.env.SOLANA_EXECUTOR_ENABLED === "true",
  live: process.env.SOLANA_LIVE_ENABLED === "true" && process.env.SOLANA_LIVE_ACK === LIVE_ACK,
  probeUsd: Math.min(3, Math.max(1, Number(process.env.SOLANA_MAX_ENTRY_USD || 3))),
  totalUsd: Math.min(6, Math.max(3, Number(process.env.SOLANA_MAX_TOTAL_EXPOSURE_USD || 6))),
  stopPct: Math.min(.25, Math.max(.08, Number(process.env.SOLANA_STOP_LOSS_PCT || .18))),
  targetPct: Math.min(1, Math.max(.15, Number(process.env.SOLANA_TAKE_PROFIT_PCT || .40))),
  trailPct: Math.min(.25, Math.max(.08, Number(process.env.SOLANA_TRAILING_STOP_PCT || .15))),
  maxPositions: Math.min(2, Math.max(1, Number(process.env.SOLANA_MAX_POSITIONS || 2))),
  minPaper: Math.max(20, Number(process.env.SOLANA_MIN_PAPER_OBSERVATIONS || 50)),
};

function blank() { return {version: 1, createdAt: new Date().toISOString(), paperObservations: 0, positions: [], fills: [], errors: [], seen: {}}; }
function load() { try { return {...blank(), ...JSON.parse(fs.readFileSync(DATA_PATH, "utf8"))}; } catch { return blank(); } }
let state = load();
function save() { fs.mkdirSync(DATA_PATH.substring(0, DATA_PATH.lastIndexOf("/")), {recursive: true}); const tmp = `${DATA_PATH}.tmp`; fs.writeFileSync(tmp, JSON.stringify(state)); fs.renameSync(tmp, DATA_PATH); }
function sanitized() { const address=wallet?.publicKey.toString() || ""; return {...state, seen: undefined, walletSuffix: address ? address.slice(-6) : "", live: cfg.live,
  ready: readiness().length === 0, blockers: readiness(), limits: {entryUsd: cfg.probeUsd, totalUsd: cfg.totalUsd,
  maxPositions: cfg.maxPositions, stopPct: cfg.stopPct, targetPct: cfg.targetPct, trailPct: cfg.trailPct}}; }

let wallet = null;
try { if (cfg.secret) wallet = Keypair.fromSecretKey(bs58.decode(cfg.secret)); } catch (e) { state.errors.push(`wallet key invalid: ${e.message}`); }
function readiness() {
  const b=[];
  if (!cfg.enabled) b.push("SOLANA_EXECUTOR_ENABLED is not true");
  if (!cfg.discovery) b.push("SOLANA_DISCOVERY_URL missing");
  if (!cfg.apiKey) b.push("JUPITER_API_KEY missing");
  if (!cfg.heliusKey) b.push("HELIUS_API_KEY missing");
  if (!wallet) b.push("SOLANA_WALLET_PRIVATE_KEY missing or invalid");
  if (!cfg.live) b.push("live acknowledgement not armed");
  if (state.paperObservations < cfg.minPaper) b.push(`paper observations ${state.paperObservations}/${cfg.minPaper}`);
  if (Date.now()-Date.parse(state.createdAt)<24*3600e3) b.push("24-hour paper soak not completed");
  return b;
}
async function json(url, options={}) { const r=await fetch(url, options); if (!r.ok) throw new Error(`${r.status} ${await r.text()}`); return r.json(); }
async function rpc(method, params) { return json(`https://mainnet.helius-rpc.com/?api-key=${encodeURIComponent(cfg.heliusKey)}`,
  {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({jsonrpc:"2.0",id:1,method,params})}); }
async function balances() {
  const owner=wallet.publicKey.toString();
  const [sol,tokens]=await Promise.all([rpc("getBalance",[owner]),rpc("getTokenAccountsByOwner",[owner,{mint:USDC},{encoding:"jsonParsed"}])]);
  const usdc=(tokens.result?.value||[]).reduce((n,x)=>n+Number(x.account?.data?.parsed?.info?.tokenAmount?.uiAmount||0),0);
  state.balances={sol:Number(sol.result?.value||0)/1e9,usdc,checkedAt:new Date().toISOString()};
  return state.balances;
}
async function order(inputMint, outputMint, amount) {
  const q = new URLSearchParams({inputMint, outputMint, amount: String(amount), taker: wallet.publicKey.toString()});
  return json(`https://api.jup.ag/swap/v2/order?${q}`, {headers:{"x-api-key":cfg.apiKey}});
}
async function execute(o) {
  if (!o.transaction) throw new Error(`unbuildable order ${o.router || ""}/${o.errorCode || ""}`);
  const tx=VersionedTransaction.deserialize(Buffer.from(o.transaction,"base64")); tx.sign([wallet]);
  return json("https://api.jup.ag/swap/v2/execute", {method:"POST", headers:{"content-type":"application/json","x-api-key":cfg.apiKey},
    body:JSON.stringify({signedTransaction:Buffer.from(tx.serialize()).toString("base64"),requestId:o.requestId,lastValidBlockHeight:o.lastValidBlockHeight})});
}
function exposure() { return state.positions.reduce((n,p)=>n+p.entryUsd,0); }
async function buy(c) {
  const o=await order(USDC,c.mint,Math.round(cfg.probeUsd*1e6)); const result=await execute(o);
  if (result.status!=="Success" || Number(result.code)!==0) throw new Error(`buy failed: ${result.error || result.code}`);
  const qty=Number(result.totalOutputAmount); const fill={id:result.signature,action:"BUY",mint:c.mint,symbol:c.symbol,
    at:new Date().toISOString(),inputUnits:Number(result.totalInputAmount),outputUnits:qty,score:c.score};
  state.fills.unshift(fill); state.positions.push({mint:c.mint,symbol:c.symbol,decimals:c.decimals,quantity:qty,entryUsd:cfg.probeUsd,
    entryPrice:Number(c.price_usd),highPrice:Number(c.price_usd),openedAt:fill.at,entrySignature:result.signature}); save();
}
async function sell(p, reason, preparedOrder=null) {
  const o=preparedOrder || await order(p.mint,USDC,Math.floor(p.quantity)); const result=await execute(o);
  if (result.status!=="Success" || Number(result.code)!==0) throw new Error(`sell failed: ${result.error || result.code}`);
  const proceeds=Number(result.totalOutputAmount)/1e6; const fill={id:result.signature,action:"SELL",reason,mint:p.mint,symbol:p.symbol,
    at:new Date().toISOString(),inputUnits:Number(result.totalInputAmount),outputUnits:Number(result.totalOutputAmount),
    realizedPnlUsd:proceeds-p.entryUsd}; state.fills.unshift(fill); state.positions=state.positions.filter(x=>x.entrySignature!==p.entrySignature); save();
}
async function tick() {
  if (!cfg.enabled || !cfg.discovery) return;
  const report=await json(`${cfg.discovery}/candidates`); const candidates=report.candidates || [];
  for (const c of candidates.filter(x=>x.qualified)) {
    const key=String(c.mint); if (!state.seen[key]) { state.seen[key]=report.scanned_at; state.paperObservations++; }
  }
  if (readiness().length===0) {
    const cash=await balances();
    if (cash.usdc<cfg.probeUsd || cash.sol<0.005) throw new Error(`funding insufficient: need at least $${cfg.probeUsd} USDC and 0.005 SOL`);
    for (const p of [...state.positions]) { const exitOrder=await order(p.mint,USDC,Math.floor(p.quantity));
      const markUsd=Number(exitOrder.outAmount||0)/1e6; if (!markUsd) continue;
      p.highUsd=Math.max(p.highUsd||p.entryUsd,markUsd); const change=markUsd/p.entryUsd-1;
      if (change<=-cfg.stopPct) await sell(p,"STOP_LOSS",exitOrder);
      else if (change>=cfg.targetPct) await sell(p,"TAKE_PROFIT",exitOrder);
      else if (markUsd<=p.highUsd*(1-cfg.trailPct) && p.highUsd>p.entryUsd) await sell(p,"TRAILING_STOP",exitOrder);
      else if (Date.now()-Date.parse(p.openedAt)>=24*3600e3) await sell(p,"MAX_HOLD_24H",exitOrder);
    }
    for (const c of candidates.filter(x=>x.qualified)) { if (state.positions.length>=cfg.maxPositions || exposure()+cfg.probeUsd>cfg.totalUsd) break;
      if (!state.positions.some(p=>p.mint===c.mint)) await buy(c);
    }
  }
  save();
}

setInterval(()=>tick().catch(e=>{state.errors.unshift(`${new Date().toISOString()} ${e.message}`);state.errors=state.errors.slice(0,20);save();}),
  Math.max(10,Number(process.env.SOLANA_EXECUTOR_INTERVAL_SECONDS||20))*1000);
http.createServer((req,res)=>{if (!["/health","/status"].includes(req.url)){res.writeHead(404).end();return;} const body=JSON.stringify(req.url==="/health"?{service:"solana-executor",...sanitized(),positions:undefined,fills:undefined,errors:state.errors.slice(0,3)}:sanitized()); res.writeHead(200,{"content-type":"application/json"});res.end(body);}).listen(Number(process.env.PORT||8080));
tick().catch(e=>{state.errors.unshift(`${new Date().toISOString()} ${e.message}`);save();});
