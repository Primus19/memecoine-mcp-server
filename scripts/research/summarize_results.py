"""Compact the raw research JSON (strip per-trade dumps) and print markdown tables.
Usage: .venv/bin/python scripts/research/summarize_results.py"""
import json, os, sys
D = "docs/research"

def load(name):
    p = os.path.join(D, name)
    return json.load(open(p)) if os.path.exists(p) else None

def compact():
    fr = load("forex_replay_results.json")
    if fr:
        for k in list(fr):
            if k.endswith("_trades"):
                fr[k] = {s: t[-50:] for s, t in fr[k].items()}
        json.dump(fr, open(os.path.join(D, "forex_replay_results.json"), "w"), indent=1, default=str)
    cr = load("crypto_replay_results.json")
    if cr:
        cr["trades"] = {k: v[-50:] for k, v in cr["trades"].items()}
        json.dump(cr, open(os.path.join(D, "crypto_replay_results.json"), "w"), indent=1, default=str)
    sb = load("strategy_backtest_results.json")
    if sb:
        for k, v in sb.items():
            if isinstance(v, dict) and "trades" in v:
                v["trades"] = v["trades"][-50:]
        json.dump(sb, open(os.path.join(D, "strategy_backtest_results.json"), "w"), indent=1, default=str)

def forex_table():
    fr = load("forex_replay_results.json")
    if not fr:
        return "Forex replay results not available."
    lines = ["| Sleeve | Spread | Signals | Trades | Win rate | Avg net bps | Expectancy (R) | Profit factor | 95% LCB bps | Exit reasons |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(k for k in fr if not k.endswith("_trades")):
        spread = key.split("_")[1]
        for sleeve, s in fr[key].items():
            exits = ", ".join(f"{k} {v}" for k, v in list(s.get("exit_reasons", {}).items())[:4])
            lines.append(f"| {sleeve} | {spread} | {s['signals_seen']} | {s['trades']} | {s['win_rate']} | {s['avg_return_bps']} | {s['expectancy_r']} | {s['profit_factor']} | {s['lower_confidence_bound_bps']} | {exits} |")
    return "\n".join(lines)

def strategy_table():
    sb = load("strategy_backtest_results.json")
    if not sb:
        return "Strategy backtest results not available."
    lines = ["| Run | Trades | CAGR | Sharpe | Max DD | Win rate | PF | Avg bps/trade |", "|---|---|---|---|---|---|---|---|"]
    for k, v in sb.items():
        if isinstance(v, dict) and "metrics" in v:
            m = v["metrics"]
            lines.append(f"| {k} | {m.get('trades')} | {m.get('cagr_pct')}% | {m.get('sharpe')} | {m.get('max_drawdown_pct')}% | {m.get('win_rate')} | {m.get('profit_factor')} | {m.get('avg_trade_return_bps')} |")
    ml = {k: v.get("ml_reports") for k, v in sb.items() if isinstance(v, dict) and v.get("ml_reports")}
    for k, reports in ml.items():
        accs = [r["oos_accuracy"] for r in reports.values() if r.get("oos_accuracy") is not None]
        base = [r["baseline_accuracy"] for r in reports.values() if r.get("baseline_accuracy") is not None]
        if accs:
            lines.append(f"\nML veto out-of-sample ({k}): mean accuracy {sum(accs)/len(accs):.3f} vs base rate {sum(base)/len(base):.3f} across {len(accs)} markets.")
    for k in ("walk_forward_crypto_ensemble_26bps", "walk_forward_fx_ensemble"):
        if k in sb:
            lines.append(f"\nWalk-forward {k}: " + "; ".join(f"{w['test_start'][:7]}..{w['test_end'][:7]}: {w['trades']} trades, avg {w['avg_return_bps']} bps" for w in sb[k]))
    return "\n".join(lines)

if __name__ == "__main__":
    compact()
    print(forex_table()); print(); print(strategy_table())
