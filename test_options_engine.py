"""
test_options_engine.py
======================
Unit tests for OptionsPaperEngine accounting (the money-math fixes).

Run:
    python -m unittest test_options_engine -v

Uses stdlib unittest only (no extra dependency). Each test gets a fresh
engine pointed at an isolated temp state file so tests never touch the real
options_paper_account.json.
"""

import os
import tempfile
import unittest

import options_paper_engine as ope
from options_paper_engine import OptionsPaperEngine, _pos_key, LOT_SIZE


class EngineTestBase(unittest.TestCase):
    """Builds a fresh, isolated engine for every test."""

    def setUp(self):
        # Redirect the engine's state file to a throwaway temp path.
        fd, self._state_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self._state_path)  # start with no file so DEFAULT_STATE is used
        self._orig_state_file = ope.STATE_FILE
        ope.STATE_FILE = self._state_path
        self.engine = OptionsPaperEngine()
        self.start_capital = self.engine.state["capital"]

    def tearDown(self):
        ope.STATE_FILE = self._orig_state_file
        if os.path.exists(self._state_path):
            os.remove(self._state_path)


class TestLotSizes(EngineTestBase):
    def test_current_lot_sizes(self):
        self.assertEqual(LOT_SIZE["NIFTY"], 65)
        self.assertEqual(LOT_SIZE["BANKNIFTY"], 30)
        self.assertEqual(LOT_SIZE["SENSEX"], 20)


class TestBuyAccounting(EngineTestBase):
    def test_buy_deducts_cost_and_close_settles_pnl(self):
        # BUY 1 lot NIFTY 24500 CE @ 100 -> cost = 100 * 65 = 6500
        ok, _ = self.engine.place_order(
            "NIFTY", 24500, "CE", "26JUN", "BUY", 1, 100.0
        )
        self.assertTrue(ok)
        qty = 1 * LOT_SIZE["NIFTY"]
        self.assertAlmostEqual(
            self.engine.state["capital"], self.start_capital - 100.0 * qty
        )

        # Close at 150 -> profit = (150-100)*65 = 3250
        key = _pos_key("NIFTY", 24500, "CE", "26JUN")
        ok, _ = self.engine.close_position(key, 150.0)
        self.assertTrue(ok)
        expected_profit = (150.0 - 100.0) * qty
        self.assertAlmostEqual(self.engine.state["realized_pnl"], expected_profit)
        # Capital back to start + profit
        self.assertAlmostEqual(
            self.engine.state["capital"], self.start_capital + expected_profit
        )
        self.assertEqual(len(self.engine.get_open_positions()), 0)


class TestSellAccounting(EngineTestBase):
    def test_sell_credit_not_added_to_free_capital(self):
        avail_before = self.engine.get_available_capital()
        ok, _ = self.engine.place_order(
            "NIFTY", 24500, "CE", "26JUN", "SELL", 1, 120.0
        )
        self.assertTrue(ok)

        # Credit must NOT inflate free capital; it lives in premium_received.
        qty = 1 * LOT_SIZE["NIFTY"]
        self.assertAlmostEqual(self.engine.state["premium_received"], 120.0 * qty)

        # Available capital should have DECREASED by the blocked margin only,
        # never increased by the premium.
        self.assertLess(self.engine.get_available_capital(), avail_before)

    def test_sell_close_releases_credit_and_settles_pnl(self):
        self.engine.place_order("NIFTY", 24500, "CE", "26JUN", "SELL", 1, 120.0)
        cap_after_open = self.engine.state["capital"]
        qty = 1 * LOT_SIZE["NIFTY"]

        # Buy back cheaper at 80 -> profit = (120-80)*65 = 2600
        key = _pos_key("NIFTY", 24500, "CE", "26JUN")
        ok, _ = self.engine.close_position(key, 80.0)
        self.assertTrue(ok)
        expected_profit = (120.0 - 80.0) * qty
        self.assertAlmostEqual(self.engine.state["realized_pnl"], expected_profit)
        # premium_received bucket fully released back to 0
        self.assertAlmostEqual(self.engine.state["premium_received"], 0.0)
        # Margin freed
        self.assertAlmostEqual(self.engine.state["used_margin"], 0.0)
        # Capital increased by the realized profit
        self.assertAlmostEqual(
            self.engine.state["capital"], cap_after_open + expected_profit
        )


class TestSpreadMargin(EngineTestBase):
    def test_hedged_short_blocks_spread_maxloss_not_full_span(self):
        # Long leg first (hedge): BUY 24600 CE
        self.engine.place_order(
            "NIFTY", 24600, "CE", "26JUN", "BUY", 1, 60.0, strategy_tag="bear_call"
        )
        # Short leg hedged by the long: SELL 24500 CE in same strategy
        self.engine.place_order(
            "NIFTY", 24500, "CE", "26JUN", "SELL", 1, 100.0, strategy_tag="bear_call"
        )
        qty = 1 * LOT_SIZE["NIFTY"]
        width = abs(24500 - 24600)
        expected = max(0.0, (width - 100.0) * qty)  # (100-100)*65 = 0 here
        self.assertAlmostEqual(self.engine.state["used_margin"], expected)
        # Crucially, NOT the full naked SPAN margin.
        self.assertNotEqual(
            self.engine.state["used_margin"], ope.MARGIN_PER_LOT["NIFTY"]
        )

    def test_naked_short_blocks_full_margin(self):
        self.engine.place_order("NIFTY", 24500, "CE", "26JUN", "SELL", 1, 100.0)
        self.assertAlmostEqual(
            self.engine.state["used_margin"], ope.MARGIN_PER_LOT["NIFTY"]
        )


class TestTriggers(EngineTestBase):
    def test_buy_sl_and_tp_triggers(self):
        self.engine.place_order("NIFTY", 24500, "CE", "26JUN", "BUY", 1, 100.0)
        key = _pos_key("NIFTY", 24500, "CE", "26JUN")
        self.engine.set_sl(key, 80.0)
        self.engine.set_tp(key, 130.0)

        self.assertIsNone(self.engine.update_ltp(key, 110.0))  # in between
        self.assertTrue(self.engine.update_ltp(key, 75.0).startswith("SL_HIT"))
        # Re-arm and test TP
        self.engine.set_sl(key, 80.0)
        self.assertTrue(self.engine.update_ltp(key, 135.0).startswith("TP_HIT"))

    def test_sell_sl_and_tp_triggers(self):
        self.engine.place_order("NIFTY", 24500, "CE", "26JUN", "SELL", 1, 100.0)
        key = _pos_key("NIFTY", 24500, "CE", "26JUN")
        # For a short: SL is premium rising, TP is premium falling.
        self.engine.set_sl(key, 150.0)
        self.engine.set_tp(key, 50.0)
        self.assertTrue(self.engine.update_ltp(key, 160.0).startswith("SL_HIT"))
        self.engine.set_sl(key, 150.0)
        self.assertTrue(self.engine.update_ltp(key, 40.0).startswith("TP_HIT"))


class TestDailyLossHalt(EngineTestBase):
    def test_open_drawdown_triggers_halt(self):
        # Buy a position, then mark a large unrealized loss via update_ltp.
        # Loss must exceed MAX_DAILY_LOSS_PCT * capital to halt.
        self.engine.place_order("NIFTY", 24500, "CE", "26JUN", "BUY", 1, 1000.0)
        key = _pos_key("NIFTY", 24500, "CE", "26JUN")
        # capital ~ 5,00,000; 5% = 25,000. Drop premium so loss > 25,000.
        # qty = 65; loss to exceed 25000 => drop > ~385 per share. Drop to 500.
        self.engine.update_ltp(key, 500.0)
        reason = self.engine._check_daily_loss_halt()
        self.assertIsNotNone(reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
