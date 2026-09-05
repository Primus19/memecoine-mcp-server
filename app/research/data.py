"""
Real market data providers. No random numbers live here.

All providers return a pandas DataFrame indexed by UTC timestamp with columns
open, high, low, close, volume (float). Symbols are normalized as BASE/QUOTE
(e.g. BTC/USD, EUR/USD). Timeframes: 1h, 4h, 1d.

Providers (all keyless):
  * KrakenProvider      - crypto spot OHLC (max 720 bars per call)
  * CoinbaseProvider    - crypto spot candles (300 per call, paged)
  * YahooProvider       - forex/stock/crypto via the public chart endpoint
  * FrankfurterProvider - ECB daily forex reference rates (daily only, no OHLC)
CompositeProvider tries providers in order and caches results on disk.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800}

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ai-trading-pro/2.0"})


class MarketDataError(RuntimeError):
    pass


def normalize_symbol(symbol: str) -> str:
    """BTCUSD, BTC-USD, btc/usd, BTCUSDT -> BTC/USD ; EURUSD=X -> EUR/USD"""
    s = symbol.upper().strip().replace("=X", "").replace("-", "/").replace("_", "/")
    if "/" not in s:
        for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"):
            if s.endswith(quote) and len(s) > len(quote):
                s = f"{s[:-len(quote)]}/{quote}"
                break
    base, _, quote = s.partition("/")
    if quote in ("USDT", "USDC"):
        quote = "USD"
    if base == "XBT":
        base = "BTC"
    return f"{base}/{quote}"


def _request_json(url: str, params: Optional[dict] = None, retries: int = 3, timeout: int = 20):
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            response = _SESSION.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                wait = 2 ** attempt
                logger.warning("rate limited by %s, sleeping %ss", url, wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise MarketDataError(f"request failed for {url}: {last_error}")


def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = df[OHLCV_COLUMNS].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[(df["close"] > 0) & (df["high"] >= df["low"])]
    df.index.name = "timestamp"
    return df


def _resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = {"4h": "4h", "1d": "1D", "1w": "1W", "1h": "1h"}[timeframe]
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "close"])
    return _finalize(out)


class BaseProvider:
    name = "base"
    supports = {"crypto", "forex", "stock"}

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def latest_price(self, symbol: str) -> float:
        df = self.fetch(symbol, "1h", limit=2)
        if df.empty:
            raise MarketDataError(f"no price for {symbol}")
        return float(df["close"].iloc[-1])


class KrakenProvider(BaseProvider):
    """Kraken public OHLC. https://docs.kraken.com/api/docs/rest-api/get-ohlc-data"""
    name = "kraken"
    supports = {"crypto"}
    BASE = "https://api.kraken.com/0/public"
    INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}

    @staticmethod
    def _pair(symbol: str) -> str:
        base, _, quote = normalize_symbol(symbol).partition("/")
        base = {"BTC": "XBT", "DOGE": "XDG"}.get(base, base)
        return f"{base}{quote}"

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500) -> pd.DataFrame:
        interval = self.INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataError(f"unsupported timeframe {timeframe}")
        since = int(time.time()) - TIMEFRAME_SECONDS[timeframe] * (limit + 2)
        payload = _request_json(f"{self.BASE}/OHLC", {"pair": self._pair(symbol), "interval": interval, "since": since})
        if payload.get("error"):
            raise MarketDataError(f"kraken: {payload['error']}")
        result = payload.get("result", {})
        rows = next((v for k, v in result.items() if k != "last"), [])
        if not rows:
            raise MarketDataError(f"kraken returned no candles for {symbol}")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vwap", "volume", "count"])
        df.index = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
        # Kraken's last candle is the still-forming one; drop it so we only see closed bars.
        df = df.iloc[:-1] if len(df) > 1 else df
        return _finalize(df).tail(limit)

    def latest_price(self, symbol: str) -> float:
        payload = _request_json(f"{self.BASE}/Ticker", {"pair": self._pair(symbol)})
        result = payload.get("result", {})
        for value in result.values():
            return float(value["c"][0])
        raise MarketDataError(f"kraken ticker missing for {symbol}")

    def spread_bps(self, symbol: str) -> float:
        payload = _request_json(f"{self.BASE}/Ticker", {"pair": self._pair(symbol)})
        for value in payload.get("result", {}).values():
            ask, bid = float(value["a"][0]), float(value["b"][0])
            mid = (ask + bid) / 2
            return (ask - bid) / mid * 10_000 if mid else 0.0
        return 0.0


class CoinbaseProvider(BaseProvider):
    """Coinbase Exchange public candles (max 300 per request, paged backwards)."""
    name = "coinbase"
    supports = {"crypto"}
    BASE = "https://api.exchange.coinbase.com"
    GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

    @staticmethod
    def _product(symbol: str) -> str:
        return normalize_symbol(symbol).replace("/", "-")

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500) -> pd.DataFrame:
        gran = self.GRANULARITY.get(timeframe)
        if gran is None:
            raise MarketDataError(f"unsupported timeframe {timeframe}")
        end = datetime.now(timezone.utc)
        frames: List[pd.DataFrame] = []
        remaining = limit + 1
        while remaining > 0:
            batch = min(300, remaining)
            start = end - timedelta(seconds=gran * batch)
            rows = _request_json(
                f"{self.BASE}/products/{self._product(symbol)}/candles",
                {"granularity": gran, "start": start.isoformat(), "end": end.isoformat()},
            )
            if not rows:
                break
            df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
            df.index = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
            frames.append(df)
            remaining -= batch
            end = start
            time.sleep(0.2)
        if not frames:
            raise MarketDataError(f"coinbase returned no candles for {symbol}")
        df = _finalize(pd.concat(frames))
        # drop the forming bar
        cutoff = pd.Timestamp.now(tz="UTC").floor(f"{gran}s")
        df = df[df.index < cutoff]
        return df.tail(limit)

    def latest_price(self, symbol: str) -> float:
        payload = _request_json(f"{self.BASE}/products/{self._product(symbol)}/ticker")
        return float(payload["price"])


class YahooProvider(BaseProvider):
    """Yahoo Finance chart endpoint. Works for forex (EURUSD=X), stocks and crypto (BTC-USD)."""
    name = "yahoo"
    supports = {"forex", "stock", "crypto"}
    BASE = "https://query2.finance.yahoo.com/v8/finance/chart/"

    @staticmethod
    def _ticker(symbol: str, asset_class: str) -> str:
        norm = normalize_symbol(symbol)
        base, _, quote = norm.partition("/")
        if asset_class == "forex":
            if base == "XAU" and quote == "USD":
                return "GC=F"   # COMEX gold front month (Yahoo has no spot XAUUSD)
            if base == "XAG" and quote == "USD":
                return "SI=F"
            return f"{base}{quote}=X"
        if asset_class == "crypto":
            return f"{base}-{quote}"
        return base

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500, asset_class: str = "forex") -> pd.DataFrame:
        interval = {"1h": "1h", "4h": "1h", "1d": "1d", "1w": "1wk"}.get(timeframe)
        if interval is None:
            raise MarketDataError(f"unsupported timeframe {timeframe}")
        bars = limit * (4 if timeframe == "4h" else 1) + 5
        seconds = TIMEFRAME_SECONDS["1h" if timeframe in ("1h", "4h") else timeframe]
        # Yahoo caps hourly history at ~730 days.
        span_days = min(int(bars * seconds / 86400 * (7 / 5 if asset_class != "crypto" else 1)) + 3, 729 if interval == "1h" else 20000)
        payload = _request_json(
            f"{self.BASE}{self._ticker(symbol, asset_class)}",
            {"range": f"{span_days}d", "interval": interval, "includePrePost": "false"},
        )
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise MarketDataError(f"yahoo returned nothing for {symbol}: {payload.get('chart', {}).get('error')}")
        node = result[0]
        ts = node.get("timestamp") or []
        quote = (node.get("indicators", {}).get("quote") or [{}])[0]
        if not ts or not quote:
            raise MarketDataError(f"yahoo returned no candles for {symbol}")
        df = pd.DataFrame({k: quote.get(k) for k in OHLCV_COLUMNS}, index=pd.to_datetime(ts, unit="s", utc=True))
        df = df.dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0.0)
        df = _finalize(df)
        if timeframe == "4h":
            df = _resample(df, "4h")
        # drop a forming bar
        cutoff = pd.Timestamp.now(tz="UTC").floor(f"{TIMEFRAME_SECONDS[timeframe]}s")
        df = df[df.index < cutoff]
        return df.tail(limit)


class FrankfurterProvider(BaseProvider):
    """ECB daily reference rates via frankfurter.dev. Daily closes only (no intraday, no OHLC)."""
    name = "frankfurter"
    supports = {"forex"}
    BASE = "https://api.frankfurter.dev/v1"

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500) -> pd.DataFrame:
        if timeframe != "1d":
            raise MarketDataError("frankfurter only supports daily data")
        base, _, quote = normalize_symbol(symbol).partition("/")
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=int(limit * 1.5) + 7)
        payload = _request_json(f"{self.BASE}/{start.isoformat()}..{end.isoformat()}", {"from": base, "to": quote})
        rates = payload.get("rates") or {}
        if not rates:
            raise MarketDataError(f"frankfurter returned no rates for {symbol}")
        series = pd.Series({pd.Timestamp(day, tz="UTC"): float(v[quote]) for day, v in rates.items()}).sort_index()
        df = pd.DataFrame({"open": series.shift(1).fillna(series), "high": series, "low": series, "close": series, "volume": 0.0})
        df["high"] = df[["open", "close"]].max(axis=1)
        df["low"] = df[["open", "close"]].min(axis=1)
        return _finalize(df).tail(limit)


class CompositeProvider(BaseProvider):
    """Tries providers in order per asset class, caches to disk (CSV) with a TTL."""
    name = "composite"

    def __init__(self, cache_dir: Optional[str] = None, cache_ttl_seconds: int = 600):
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl_seconds
        self.kraken = KrakenProvider()
        self.coinbase = CoinbaseProvider()
        self.yahoo = YahooProvider()
        self.frankfurter = FrankfurterProvider()
        self.chain: Dict[str, List] = {
            "crypto": [self.kraken, self.coinbase, self.yahoo],
            "forex": [self.yahoo, self.frankfurter],
            "stock": [self.yahoo],
        }
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, symbol: str, timeframe: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, f"{normalize_symbol(symbol).replace('/', '_')}_{timeframe}.csv")

    def _read_cache(self, path: Optional[str], limit: int) -> Optional[pd.DataFrame]:
        if not path or not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > self.cache_ttl:
            return None
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            if len(df) >= limit:
                return _finalize(df).tail(limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache read failed %s: %s", path, exc)
        return None

    def fetch(self, symbol: str, timeframe: str = "1d", limit: int = 500, asset_class: Optional[str] = None) -> pd.DataFrame:
        from .config import asset_class_for
        asset_class = asset_class or asset_class_for(symbol)
        path = self._cache_path(symbol, timeframe)
        cached = self._read_cache(path, limit)
        if cached is not None:
            return cached
        errors = []
        chain = list(self.chain.get(asset_class, [self.yahoo]))
        if asset_class == "crypto" and limit > 720:
            # Kraken returns at most 720 candles; Coinbase pages back much further.
            chain.sort(key=lambda p: 0 if isinstance(p, CoinbaseProvider) else 1)
        for provider in chain:
            try:
                if isinstance(provider, YahooProvider):
                    df = provider.fetch(symbol, timeframe, limit, asset_class=asset_class)
                else:
                    df = provider.fetch(symbol, timeframe, limit)
                if len(df) >= min(limit, 50):
                    if path:
                        df.to_csv(path)
                    return df
                errors.append(f"{provider.name}: only {len(df)} bars")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.name}: {exc}")
        raise MarketDataError(f"all providers failed for {symbol} {timeframe}: {'; '.join(errors)}")

    def latest_price(self, symbol: str) -> float:
        from .config import asset_class_for
        asset_class = asset_class_for(symbol)
        for provider in self.chain.get(asset_class, [self.yahoo]):
            try:
                if isinstance(provider, YahooProvider):
                    df = provider.fetch(symbol, "1h", 3, asset_class=asset_class)
                    return float(df["close"].iloc[-1])
                return provider.latest_price(symbol)
            except Exception:  # noqa: BLE001
                continue
        raise MarketDataError(f"no live price for {symbol}")


def load_history(symbols: List[str], timeframe: str = "1d", bars: int = 1500,
                 cache_dir: Optional[str] = None) -> Dict[str, pd.DataFrame]:
    """Fetch history for many symbols, skipping (and logging) the ones that fail."""
    provider = CompositeProvider(cache_dir=cache_dir or os.getenv("RESEARCH_CACHE_DIR", "data/research_cache"),
                                 cache_ttl_seconds=int(os.getenv("RESEARCH_CACHE_TTL", "86400")))
    out: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            out[symbol] = provider.fetch(symbol, timeframe, bars)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping %s: %s", symbol, exc)
    return out


_default: Optional[CompositeProvider] = None


def get_default_provider(cache_dir: Optional[str] = None) -> CompositeProvider:
    global _default
    if _default is None:
        _default = CompositeProvider(cache_dir=cache_dir or os.getenv("MARKET_CACHE_DIR", "instance/market_cache"))
    return _default
