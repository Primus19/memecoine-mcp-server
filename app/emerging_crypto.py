from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence_worker import evaluate_goplus, evaluate_goplus_solana


UTC = timezone.utc
SUPPORTED_CHAINS = {"base": "base", "solana": "solana", "robinhood": "robinhood"}
DEFAULT_RESEARCH_SEEDS = (
    ("robinhood", "0x39dbed3a2bd333467115de45665cc57f813c4571"),
    ("robinhood", "0xb2000000000000000000004c27f6523082f41d01"),
    ("robinhood", "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"),
)


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "primus-emerging-crypto/1.0",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _addresses() -> list[tuple[str, str]]:
    base = os.getenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com").rstrip("/")
    endpoints = ("/token-profiles/latest/v1", "/token-profiles/recent-updates/v1", "/token-boosts/top/v1")
    # User-observed contracts are permanent research controls. Inclusion here
    # means "always evaluate", never "assume profitable" or "buy live".
    discovered: list[tuple[str, str]] = list(DEFAULT_RESEARCH_SEEDS)
    for endpoint in endpoints:
        try:
            payload = fetch_json(base + endpoint)
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            chain = str(row.get("chainId") or "").lower()
            address = str(row.get("tokenAddress") or "").strip()
            if chain in SUPPORTED_CHAINS and address:
                discovered.append((chain, address))
    configured = os.getenv("EMERGING_CRYPTO_SEED_CONTRACTS", "")
    for item in configured.split(","):
        chain, separator, address = item.strip().partition(":")
        if separator and chain.lower() in SUPPORTED_CHAINS and address:
            discovered.append((chain.lower(), address))
    return list(dict.fromkeys(discovered))


def _pairs(addresses: list[tuple[str, str]]) -> list[dict[str, Any]]:
    base = os.getenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com").rstrip("/")
    result = []
    by_chain: dict[str, list[str]] = {}
    for chain, address in addresses:
        by_chain.setdefault(chain, []).append(address)
    for chain, values in by_chain.items():
        for start in range(0, len(values), 30):
            joined = ",".join(values[start:start + 30])
            try:
                payload = fetch_json(f"{base}/tokens/v1/{chain}/{joined}")
            except Exception:
                continue
            if isinstance(payload, list):
                result.extend(row for row in payload if isinstance(row, dict))
    return result


def _best_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        chain = str(row.get("chainId") or "").lower()
        address = str((row.get("baseToken") or {}).get("address") or "").lower()
        if not chain or not address:
            continue
        key = chain, address
        if _number((row.get("liquidity") or {}).get("usd")) > _number((best.get(key, {}).get("liquidity") or {}).get("usd")):
            best[key] = row
    return list(best.values())


def _candles(chain: str, pair_address: str) -> list[dict[str, Any]]:
    base = os.getenv("GECKOTERMINAL_BASE_URL", "https://api.geckoterminal.com/api/v2").rstrip("/")
    network = {"base": "base", "solana": "solana", "robinhood": "robinhood-chain"}.get(chain, chain)
    query = urllib.parse.urlencode({"aggregate": 1, "limit": 90, "currency": "usd", "token": "base"})
    payload = fetch_json(f"{base}/networks/{network}/pools/{urllib.parse.quote(pair_address)}/ohlcv/day?{query}")
    rows = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    candles = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        stamp, opening, high, low, close, volume = row[:6]
        if _number(close) > 0:
            candles.append({"observed_at": datetime.fromtimestamp(float(stamp), UTC).isoformat(),
                            "open": _number(opening), "high": _number(high), "low": _number(low),
                            "close": _number(close), "volume_usd": _number(volume)})
    return sorted(candles, key=lambda item: item["observed_at"])


def _security(chain: str, address: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"contract_addresses": address})
    if chain == "base":
        payload = fetch_json(f"https://api.gopluslabs.io/api/v1/token_security/8453?{encoded}")
        facts = (payload.get("result") or {}).get(address.lower()) or (payload.get("result") or {}).get(address) or {}
        clean, _, failures = evaluate_goplus(facts)
        holders = facts.get("holders") or []
        top10 = sum(_number(item.get("percent")) for item in holders[:10]) if holders else None
        creator = max(_number(facts.get("creator_percent"), -1), _number(facts.get("owner_percent"), -1))
        return {"security_verified": clean, "security_failures": failures,
                "top10_holder_fraction": top10, "creator_fraction": creator if creator >= 0 else None,
                "security_source_url": "https://gopluslabs.io/token-security/8453"}
    if chain == "solana":
        payload = fetch_json(f"https://api.gopluslabs.io/api/v1/solana/token_security?{encoded}")
        facts = (payload.get("result") or {}).get(address) or {}
        clean, _, failures = evaluate_goplus_solana(facts)
        holders = facts.get("holders") or []
        top10 = sum(_number(item.get("percent")) for item in holders[:10]) if holders else None
        creators = facts.get("creators") or facts.get("creator") or []
        creator = sum(_number(item.get("percent")) for item in creators if isinstance(item, dict)) or None
        return {"security_verified": clean, "security_failures": failures,
                "top10_holder_fraction": top10, "creator_fraction": creator,
                "security_source_url": "https://gopluslabs.io/token-security/solana"}
    return {"security_verified": False, "security_failures": ["chain safety adapter unavailable"],
            "top10_holder_fraction": None, "creator_fraction": None, "security_source_url": ""}


def _execution(liquidity_usd: float, probe_usd: float, fee_bps: float) -> dict[str, Any]:
    # Constant-product pools are modeled conservatively as half the displayed
    # USD liquidity per side. This is paper-only and never authorizes live execution.
    side_liquidity = liquidity_usd / 2
    one_way_impact = probe_usd / max(side_liquidity + probe_usd, 1e-9)
    round_trip_impact = min(.99, one_way_impact * 2)
    fee = max(0.0, fee_bps) / 10_000
    recovery = (1 - round_trip_impact) * (1 - fee) ** 2
    return {"sell_route_ok": liquidity_usd > probe_usd * 20,
            "round_trip_recovery": recovery,
            "sell_impact_bps": round_trip_impact * 10_000,
            "spread_bps": 0.0, "estimated_slippage_bps": one_way_impact * 10_000,
            "execution_probe_usd": probe_usd, "modeled_fee_bps_per_side": fee_bps,
            "execution_evidence_mode": "DEX_AMM_STRESS_MODEL"}


def refresh_held_position_quotes(positions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Refresh only held on-chain assets for paper risk monitoring.

    Full-universe discovery is intentionally slow and expensive.  Open-position
    risk is different: it must not reuse a six-hour-old discovery snapshot.
    DexScreener supplies a fresh pool observation and we apply the same
    conservative full-position AMM/fee stress model used at admission.
    """
    wanted = [(str(item.get("chain") or "").lower(), str(item.get("contract") or ""),
               str(item.get("symbol") or "")) for item in positions]
    wanted = list(dict.fromkeys(item for item in wanted if item[0] in SUPPORTED_CHAINS and item[1]))
    if not wanted:
        return [], []
    probe_default = max(10.0, float(os.getenv("EMERGING_CRYPTO_EXECUTION_PROBE_USD", "100")))
    fee_bps = max(0.0, float(os.getenv("EMERGING_CRYPTO_MODELED_FEE_BPS_PER_SIDE", "60")))
    rows = _best_pairs(_pairs([(chain, contract) for chain, contract, _ in wanted]))
    by_identity = {(str(row.get("chainId") or "").lower(),
                    str((row.get("baseToken") or {}).get("address") or "").lower()): row for row in rows}
    refreshed, errors = [], []
    observed_at = datetime.now(UTC).isoformat()
    for chain, contract, symbol in wanted:
        pair = by_identity.get((chain, contract.lower()))
        if not pair:
            errors.append({"chain": chain, "contract": contract, "symbol": symbol,
                           "error": "held-position quote unavailable"})
            continue
        price = _number(pair.get("priceUsd"))
        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        if price <= 0 or liquidity <= 0:
            errors.append({"chain": chain, "contract": contract, "symbol": symbol,
                           "error": "held-position price or liquidity unavailable"})
            continue
        position = next(item for item in positions
                        if str(item.get("chain") or "").lower() == chain and
                        str(item.get("contract") or "").lower() == contract.lower())
        entry_value = float(position.get("fill_price") or 0) * float(position.get("quantity") or 0)
        execution = _execution(liquidity, max(probe_default, entry_value), fee_bps)
        one_way_cost = execution["estimated_slippage_bps"] / 10_000 + fee_bps / 10_000
        refreshed.append({
            "asset_class": "CRYPTO", "chain": chain, "contract": contract,
            "symbol": symbol, "price": price,
            "executable_sell_price": price * (1 - one_way_cost),
            "market_cap_usd": _number(pair.get("marketCap") or pair.get("fdv")),
            "liquidity_usd": liquidity,
            "volume_24h_usd": _number((pair.get("volume") or {}).get("h24")),
            "observed_at": observed_at, "quote_age_seconds": 0.0,
            "tradable": True, "market_veto": False,
            "source_urls": [str(pair.get("url") or "")],
            "price_source": "DEXSCREENER_HELD_POSITION_AMM_STRESS_MARK",
            **execution,
        })
    return refreshed, errors


def _retained_candles(chain: str, address: str, price: float, volume: float) -> list[dict[str, Any]]:
    """Retain one honest daily observation when an upstream OHLCV API lacks a chain."""
    path = Path(os.getenv("EMERGING_CRYPTO_HISTORY_PATH", "/app/data/emerging_crypto_history.json"))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        state = {}
    key = f"{chain}:{address.lower()}"
    rows = list(state.get(key) or [])
    now = datetime.now(UTC)
    day = now.date().isoformat()
    observation = {"observed_at": now.isoformat(), "open": price, "high": price,
                   "low": price, "close": price, "volume_usd": volume,
                   "source": "DEXSCREENER_DAILY_OBSERVATION"}
    if rows and str(rows[-1].get("observed_at") or "")[:10] == day:
        prior = rows[-1]
        observation.update(open=_number(prior.get("open"), price),
                           high=max(_number(prior.get("high"), price), price),
                           low=min(_number(prior.get("low"), price), price))
        rows[-1] = observation
    else:
        rows.append(observation)
    rows = rows[-120:]
    state[key] = rows
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
    return rows


def emerging_crypto_universe() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if os.getenv("EMERGING_CRYPTO_DISCOVERY_ENABLED", "true").lower() != "true":
        return [], {"status": "DISABLED", "candidate_count": 0}
    limit = max(5, min(40, int(os.getenv("EMERGING_CRYPTO_UNIVERSE_SIZE", "20"))))
    probe = max(10.0, float(os.getenv("EMERGING_CRYPTO_EXECUTION_PROBE_USD", "100")))
    fee_bps = max(0.0, float(os.getenv("EMERGING_CRYPTO_MODELED_FEE_BPS_PER_SIDE", "60")))
    min_liquidity = max(25_000.0, float(os.getenv("EMERGING_CRYPTO_MIN_LIQUIDITY_USD", "100000")))
    min_volume = max(25_000.0, float(os.getenv("EMERGING_CRYPTO_MIN_VOLUME_24H_USD", "250000")))
    max_age = max(30.0, float(os.getenv("EMERGING_CRYPTO_MAX_AGE_DAYS", "120")))
    rows, errors = [], []
    addresses = _addresses()
    ranked = sorted(_best_pairs(_pairs(addresses)),
                    key=lambda row: _number((row.get("volume") or {}).get("h24")), reverse=True)
    quota = max(3, min(limit, int(os.getenv(
        "EMERGING_CRYPTO_ROBINHOOD_MIN_CANDIDATES", str(max(3, limit // 3))))))
    robinhood = [row for row in ranked if str(row.get("chainId") or "").lower() == "robinhood"][:quota]
    supported = [row for row in ranked if str(row.get("chainId") or "").lower() in {"base", "solana"}]
    selected = supported[:max(0, limit - len(robinhood))] + robinhood
    selected_ids = {str(row.get("pairAddress") or "") for row in selected}
    selected.extend(row for row in ranked if str(row.get("pairAddress") or "") not in selected_ids)
    for pair in selected:
        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        volume = _number((pair.get("volume") or {}).get("h24"))
        created = _number(pair.get("pairCreatedAt"))
        age = (datetime.now(UTC).timestamp() - created / 1000) / 86400 if created > 0 else -1
        if liquidity < min_liquidity or volume < min_volume or age < 7 or age > max_age:
            continue
        chain = str(pair.get("chainId") or "").lower()
        token = pair.get("baseToken") or {}
        address = str(token.get("address") or "")
        try:
            try:
                candles = _candles(chain, str(pair.get("pairAddress") or ""))
            except Exception as exc:
                candles = _retained_candles(chain, address, _number(pair.get("priceUsd")), volume)
                errors.append({"chain": chain, "contract": address,
                               "error": f"OHLCV {type(exc).__name__}: {str(exc)[:140]}"})
            try:
                security = _security(chain, address)
            except Exception as exc:
                security = {"security_verified": False,
                            "security_failures": [f"security provider unavailable: {type(exc).__name__}"],
                            "top10_holder_fraction": None, "creator_fraction": None,
                            "security_source_url": ""}
                errors.append({"chain": chain, "contract": address,
                               "error": f"SAFETY {type(exc).__name__}: {str(exc)[:140]}"})
            price = _number(pair.get("priceUsd"))
            if not candles:
                candles = _retained_candles(chain, address, price, volume)
            sources = [str(pair.get("url") or "")]
            if security["security_source_url"]:
                sources.append(security["security_source_url"])
            execution = _execution(liquidity, probe, fee_bps)
            one_way_cost = execution["estimated_slippage_bps"] / 10_000 + fee_bps / 10_000
            rows.append({"chain": chain, "contract": address, "symbol": str(token.get("symbol") or "").upper(),
                         "name": str(token.get("name") or token.get("symbol") or ""), "price": price,
                         "executable_buy_price": price * (1 + one_way_cost),
                         "executable_sell_price": price * (1 - one_way_cost),
                         "market_cap_usd": _number(pair.get("marketCap") or pair.get("fdv")),
                         "liquidity_usd": liquidity, "volume_24h_usd": volume, "daily_candles": candles,
                         "token_age_days": age, "benchmark_return_7d_pct": 0.0,
                         "holder_growth_7d_pct": 0.0, "tradable": True, "market_veto": False,
                         "quote_age_seconds": 0.0, "initial_stop_fraction": .12,
                         "observed_at": datetime.now(UTC).isoformat(), "source_urls": [url for url in sources if url.startswith("https://")],
                         "pair_address": pair.get("pairAddress"), "evidence_status": "DEX_DISCOVERY_PAPER_ONLY",
                         **security, **execution})
            if len(rows) >= limit:
                break
        except Exception as exc:
            errors.append({"chain": chain, "contract": address, "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
        time.sleep(max(0.0, float(os.getenv("EMERGING_CRYPTO_REQUEST_SPACING_SECONDS", ".25"))))
    chain_counts: dict[str, int] = {}
    for item in rows:
        chain_counts[str(item.get("chain") or "unknown")] = chain_counts.get(
            str(item.get("chain") or "unknown"), 0) + 1
    return rows, {"status": "READY" if rows else "DEGRADED", "candidate_count": len(rows),
                  "source_count": len(addresses), "chain_counts": chain_counts,
                  "robinhood_minimum_scan_quota": quota, "errors": errors[-20:]}
