"""Tests for the M5 finance vertical (pure computation, no network)."""

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path

from freetalon.orchestrator.finance import (
    backtest_strategy,
    check_kill_switch,
    engage_kill_switch,
    load_prices,
    make_invest_handler,
    make_paper_trade_handler,
    synthetic_prices,
    train_linear,
)


def _write_prices(path: Path, rows=None):
    rows = rows if rows is not None else synthetic_prices()
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "close"])
        writer.writeheader()
        writer.writerows(rows)


class TestBacktest(unittest.TestCase):
    def test_sma_crossover_trades_and_caps_positions(self):
        result = backtest_strategy(synthetic_prices(), position_cap_pct=10.0)
        self.assertGreaterEqual(result["trade_count"], 1)
        self.assertLessEqual(result["max_position_pct"], 10.0)
        self.assertEqual(result["cap_breaches"], 0)
        self.assertIn("final_equity", result)
        self.assertIn("max_drawdown_pct", result)
        self.assertIn("sharpe", result)

    def test_buy_hold_uses_the_cap_too(self):
        result = backtest_strategy(
            synthetic_prices(), strategy="buy_hold", position_cap_pct=5.0
        )
        self.assertLessEqual(result["max_position_pct"], 5.0)
        self.assertEqual(result["trade_count"], 1)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            backtest_strategy(synthetic_prices(), strategy="moon")

    def test_load_prices_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.csv"
            _write_prices(path)
            rows = load_prices(path)
            self.assertEqual(len(rows), 60)
            self.assertEqual(rows[0]["date"], "2026-01-01")


class TestTrain(unittest.TestCase):
    def test_linear_fit_on_perfect_data(self):
        rows = [{"x": float(i), "y": 2.0 * i + 1.0} for i in range(10)]
        model = train_linear(rows, "x", "y")
        self.assertAlmostEqual(model["slope"], 2.0)
        self.assertAlmostEqual(model["intercept"], 1.0)
        self.assertAlmostEqual(model["r2"], 1.0)
        self.assertAlmostEqual(model["mae"], 0.0)

    def test_no_variance_raises(self):
        with self.assertRaises(ValueError):
            train_linear([{"x": 1.0, "y": 2.0}, {"x": 1.0, "y": 3.0}], "x", "y")


class TestPaperTrade(unittest.TestCase):
    def test_paper_mode_writes_a_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            prices = Path(tmp) / "prices.csv"
            _write_prices(prices)
            handler = make_paper_trade_handler()
            result = asyncio.run(
                handler(
                    {
                        "prices": str(prices),
                        "run_dir": tmp,
                        "position_cap_pct": 10.0,
                    }
                )
            )
            self.assertEqual(result["status"], "success")
            ledger = json.loads(Path(result["ledger_path"]).read_text())
            self.assertEqual(ledger["mode"], "paper")
            self.assertEqual(ledger["note"], "paper trade only — no real money moved")
            self.assertLessEqual(result["metrics"]["max_position_pct"], 10.0)

    def test_non_paper_mode_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_paper_trade_handler()
            with self.assertRaises(ValueError):
                asyncio.run(handler({"mode": "live", "run_dir": tmp}))

    def test_kill_switch_blocks_trading(self):
        with tempfile.TemporaryDirectory() as tmp:
            prices = Path(tmp) / "prices.csv"
            _write_prices(prices)
            engage_kill_switch(tmp, reason="test")
            handler = make_paper_trade_handler()
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    handler({"prices": str(prices), "run_dir": tmp})
                )

    def test_check_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            check_kill_switch(tmp)  # no file: passes
            engage_kill_switch(tmp)
            with self.assertRaises(RuntimeError):
                check_kill_switch(tmp)


class TestInvestStub(unittest.TestCase):
    def test_invest_always_refuses(self):
        handler = make_invest_handler()
        with self.assertRaises(RuntimeError):
            asyncio.run(handler({"amount": 1000.0}))


class TestSyntheticPrices(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(synthetic_prices(), synthetic_prices())
        self.assertEqual(len(synthetic_prices()), 60)


if __name__ == "__main__":
    unittest.main()
