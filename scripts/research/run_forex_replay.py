"""Replay the pilot's forex sleeves on hourly history. Writes docs/research/forex_replay_results.json."""
import json, logging, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
logging.basicConfig(level=logging.WARNING)
from app.research.data import load_history
from app.research.replay import replay_forex
t = time.time()
fxh = load_history(["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"], "1h", 12000, cache_dir="data/research_cache")
out = {}
for spread in (1.5, 3.0):
    res = replay_forex(fxh, spread_bps=spread, max_hold_bars=48, min_score=80.0)
    out[f"spread_{spread}"] = {k: v.summary() for k, v in res.items()}
    out[f"spread_{spread}_trades"] = {k: [tr.__dict__ for tr in v.trades[-50:]] for k, v in res.items()}
    print("done spread", spread, round(time.time() - t), flush=True)
json.dump(out, open("docs/research/forex_replay_results.json", "w"), indent=1, default=str)
print("FOREX REPLAY COMPLETE", round(time.time() - t), flush=True)
