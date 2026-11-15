"""Finance vertical (M5): backtest, train, paper-trade — dry-run only.

Rules, from the design doc and the standing safety constraints:
- Paper-trade mode is the default and the only mode: there is no broker
  code anywhere in this module. Nothing here can spend real money.
- The irreversible step (investing real money) is a gated node whose
  handler is a stub that refuses: it exists so the gate has something
  to pause on, not so it can run.
- Position caps are hard constraints in the sizing logic, not warnings.
- The kill switch is a file: if ``kill_switch`` exists in the run dir
  (or the workspace above it), paper trading refuses to run.
"""

from __future__ import annotations

import csv
import json
import math
import random
from pathlib import Path
from typing import Any

DEFAULT_POSITION_CAP_PCT = 10.0
KILL_SWITCH_NAME = "kill_switch"


def check_kill_switch(run_dir: str | Path) -> None:
    """Raise if the kill switch file exists in the run dir or workspace."""
    run_dir = Path(run_dir)
    for candidate in (run_dir / KILL_SWITCH_NAME, run_dir.parent / KILL_SWITCH_NAME):
        if candidate.exists():
            raise RuntimeError(
                f"kill switch engaged ({candidate}); refusing to trade"
            )


def engage_kill_switch(run_dir: str | Path, reason: str = "") -> Path:
    """Engage the kill switch: write the file the trading claws check."""
    path = Path(run_dir) / KILL_SWITCH_NAME
    path.write_text(
        json.dumps({"reason": reason, "engaged": True}, indent=2), encoding="utf-8"
    )
    return path


def _resolve(path_value: str, base: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else base / path


def load_prices(path: Path) -> list[dict[str, Any]]:
    """Load a prices CSV (columns: date, close). Returns [{date, close}]."""
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({"date": row["date"], "close": float(row["close"])})
    if len(rows) < 2:
        raise ValueError(f"need at least 2 price rows, got {len(rows)}")
    return rows


def _sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(window - 1, len(values)):
        out[i] = sum(values[i - window + 1 : i + 1]) / window
    return out


def backtest_strategy(
    prices: list[dict[str, Any]],
    strategy: str = "sma_crossover",
    fast: int = 5,
    slow: int = 20,
    initial_capital: float = 10_000.0,
    position_cap_pct: float = DEFAULT_POSITION_CAP_PCT,
) -> dict[str, Any]:
    """Backtest a strategy over a price series. Pure computation, no I/O.

    Sizing is capped: no single position exceeds ``position_cap_pct`` of
    capital. Returns the trade log, equity curve, and summary metrics.
    """
    closes = [p["close"] for p in prices]
    dates = [p["date"] for p in prices]
    cash = initial_capital
    shares = 0.0
    trades: list[dict[str, Any]] = []
    equity: list[float] = []
    peak = initial_capital
    max_drawdown = 0.0
    cap_breaches = 0
    max_position_pct = 0.0

    if strategy == "sma_crossover":
        fast_sma = _sma(closes, fast)
        slow_sma = _sma(closes, slow)
        signals: list[str] = ["hold"] * len(closes)
        for i in range(1, len(closes)):
            if fast_sma[i] is None or slow_sma[i] is None:
                continue
            prev_fast, prev_slow = fast_sma[i - 1], slow_sma[i - 1]
            if prev_fast is None or prev_slow is None:
                continue
            if prev_fast <= prev_slow and fast_sma[i] > slow_sma[i]:
                signals[i] = "buy"
            elif prev_fast >= prev_slow and fast_sma[i] < slow_sma[i]:
                signals[i] = "sell"
    elif strategy == "buy_hold":
        signals = ["buy"] + ["hold"] * (len(closes) - 1)
    else:
        raise ValueError(f"unknown strategy {strategy!r}")

    for i, (date, close) in enumerate(zip(dates, closes)):
        if signals[i] == "buy" and shares == 0:
            # Position size is capped: the cap is a hard constraint.
            budget = cash * (position_cap_pct / 100.0)
            affordable = cash
            spend = min(budget, affordable)
            if spend <= 0:
                cap_breaches += 1
            else:
                shares = spend / close
                cash -= spend
                max_position_pct = max(max_position_pct, spend / initial_capital * 100)
                trades.append({"date": date, "side": "buy", "price": close, "shares": round(shares, 6)})
        elif signals[i] == "sell" and shares > 0:
            cash += shares * close
            trades.append({"date": date, "side": "sell", "price": close, "shares": round(shares, 6)})
            shares = 0.0
        value = cash + shares * close
        equity.append(round(value, 2))
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak if peak else 0.0)

    final = equity[-1]
    wins = 0
    round_trips = 0
    entry: float | None = None
    for trade in trades:
        if trade["side"] == "buy":
            entry = trade["price"]
        elif trade["side"] == "sell" and entry is not None:
            round_trips += 1
            if trade["price"] > entry:
                wins += 1
            entry = None
    returns = [
        (equity[i] / equity[i - 1] - 1) for i in range(1, len(equity)) if equity[i - 1] > 0
    ]
    mean_ret = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - mean_ret) ** 2 for r in returns) / len(returns) if returns else 0.0
    sharpe = (mean_ret / math.sqrt(var)) if var > 0 else 0.0
    return {
        "strategy": strategy,
        "trades": trades,
        "equity_curve": equity,
        "final_equity": round(final, 2),
        "total_return_pct": round((final / initial_capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "trade_count": len(trades),
        "win_rate": round(wins / round_trips, 3) if round_trips else 0.0,
        "sharpe": round(sharpe, 3),
        "max_position_pct": round(max_position_pct, 2),
        "position_cap_pct": position_cap_pct,
        "cap_breaches": cap_breaches,
    }


def train_linear(
    rows: list[dict[str, Any]], feature: str, target: str
) -> dict[str, Any]:
    """Single-feature linear regression, closed form. No new dependencies."""
    xs = [float(r[feature]) for r in rows]
    ys = [float(r[target]) for r in rows]
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 rows to train")
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        raise ValueError(f"feature {feature!r} has no variance")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    preds = [slope * x + intercept for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, preds))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    mae = sum(abs(y - p) for y, p in zip(ys, preds)) / n
    return {
        "feature": feature,
        "target": target,
        "slope": slope,
        "intercept": intercept,
        "r2": round(r2, 4),
        "mae": round(mae, 4),
        "n": n,
    }


# ── Claw handlers ─────────────────────────────────────────────────────────


def make_backtest_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        prices_path = _resolve(str(inputs.get("prices") or inputs.get("path") or ""), base)
        if not prices_path.is_file():
            raise ValueError(f"prices file not found: {prices_path}")
        prices = load_prices(prices_path)
        strategy = str(inputs.get("strategy", "sma_crossover"))
        result = backtest_strategy(
            prices,
            strategy=strategy,
            fast=int(inputs.get("fast", 5)),
            slow=int(inputs.get("slow", 20)),
            initial_capital=float(inputs.get("initial_capital", 10_000.0)),
            position_cap_pct=float(
                inputs.get("position_cap_pct", DEFAULT_POSITION_CAP_PCT)
            ),
        )
        out_path = base / str(inputs.get("output") or "backtest.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return {
            "status": "success",
            "report_path": str(out_path),
            "metrics": {
                "total_return_pct": result["total_return_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate": result["win_rate"],
                "max_position_pct": result["max_position_pct"],
            },
        }

    handler.__name__ = "backtest_handler"
    return handler


def make_train_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        data_path = _resolve(str(inputs.get("data") or inputs.get("path") or ""), base)
        if not data_path.is_file():
            raise ValueError(f"data file not found: {data_path}")
        with open(data_path, newline="", encoding="utf-8") as handle:
            rows = [{k: float(v) for k, v in row.items()} for row in csv.DictReader(handle)]
        feature = str(inputs.get("feature", "x"))
        target = str(inputs.get("target", "y"))
        model = train_linear(rows, feature, target)
        out_path = base / str(inputs.get("output") or "model.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(model, indent=2), encoding="utf-8")
        return {
            "status": "success",
            "model_path": str(out_path),
            "metrics": {"r2": model["r2"], "mae": model["mae"]},
        }

    handler.__name__ = "train_handler"
    return handler


def make_paper_trade_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        mode = str(inputs.get("mode", "paper"))
        if mode != "paper":
            raise ValueError(
                f"paper_trade only runs in paper mode, got {mode!r} — "
                "real-money trading is not implemented"
            )
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        check_kill_switch(base)
        prices_path = _resolve(str(inputs.get("prices") or inputs.get("path") or ""), base)
        if not prices_path.is_file():
            raise ValueError(f"prices file not found: {prices_path}")
        prices = load_prices(prices_path)
        result = backtest_strategy(
            prices,
            strategy=str(inputs.get("strategy", "sma_crossover")),
            fast=int(inputs.get("fast", 5)),
            slow=int(inputs.get("slow", 20)),
            initial_capital=float(inputs.get("initial_capital", 10_000.0)),
            position_cap_pct=float(
                inputs.get("position_cap_pct", DEFAULT_POSITION_CAP_PCT)
            ),
        )
        ledger = {
            "mode": "paper",
            "strategy": result["strategy"],
            "trades": result["trades"],
            "final_equity": result["final_equity"],
            "max_position_pct": result["max_position_pct"],
            "position_cap_pct": result["position_cap_pct"],
            "note": "paper trade only — no real money moved",
        }
        out_path = base / str(inputs.get("output") or "paper_ledger.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
        return {
            "status": "success",
            "ledger_path": str(out_path),
            "metrics": {
                "final_equity": result["final_equity"],
                "trade_count": result["trade_count"],
                "max_position_pct": result["max_position_pct"],
            },
        }

    handler.__name__ = "paper_trade_handler"
    return handler


def make_invest_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(
            "real-money investing is not implemented. This node exists only "
            "to be gated: a human must approve it, and even then there is "
            "no broker code to run."
        )

    handler.__name__ = "invest_handler"
    return handler


def make_kill_switch_handler():
    async def handler(inputs: dict[str, Any]) -> dict[str, Any]:
        base = Path(str(inputs.get("run_dir") or inputs.get("workdir") or "."))
        path = engage_kill_switch(base, reason=str(inputs.get("reason", "")))
        return {"status": "success", "kill_switch_path": str(path)}

    handler.__name__ = "kill_switch_handler"
    return handler


def register_finance_claws(registry: Any) -> None:
    """Register the finance claws on a ToolRegistry."""
    registry.register("backtest", make_backtest_handler())
    registry.register("train_model", make_train_handler())
    registry.register("paper_trade", make_paper_trade_handler())
    registry.register("invest", make_invest_handler())
    registry.register("engage_kill_switch", make_kill_switch_handler())


def synthetic_prices(seed: int = 7, days: int = 60) -> list[dict[str, Any]]:
    """Deterministic price series for tests and demos (no network)."""
    rng = random.Random(seed)
    price = 100.0
    rows: list[dict[str, Any]] = []
    for day in range(days):
        # Falls first, then rises, then falls: the SMA crossover fires a
        # buy on the turn up and a sell on the turn down.
        drift = -0.5 if day < 20 else (0.6 if day < 45 else -0.4)
        price = max(10.0, price + drift + rng.uniform(-1.5, 1.5))
        rows.append({"date": f"2026-01-{day + 1:02d}", "close": round(price, 2)})
    return rows
