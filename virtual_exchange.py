import json
import os
from datetime import datetime

STATE_FILE = "virtual_account.json"

DEFAULT_STATE = {
    "balance": 100000.0,
    "equity": 100000.0,
    "realized_pnl": 0.0,
    "open_positions": [],
    "closed_positions": []
}


class VirtualExchange:

    def __init__(self):
        self.state = self.load_state()

    def load_state(self):

        if not os.path.exists(STATE_FILE):

            with open(STATE_FILE, "w") as f:
                json.dump(DEFAULT_STATE, f, indent=2)

            return DEFAULT_STATE.copy()

        with open(STATE_FILE, "r") as f:
            return json.load(f)

    def save(self):

        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def get_balance(self):
        return self.state["balance"]

    def get_equity(self):
        return self.state["equity"]

    def buy(self, symbol, qty, price):

        cost = qty * price

        if cost > self.state["balance"]:
            return False, "Insufficient virtual funds"

        self.state["balance"] -= cost

        position = {
            "id": len(self.state["open_positions"]) + 1,
            "symbol": symbol,
            "side": "BUY",
            "qty": qty,
            "entry": price,
            "ltp": price,
            "pnl": 0,
            "opened_at": str(datetime.now())
        }

        self.state["open_positions"].append(position)

        self.save()

        return True, f"BUY {symbol} @ ₹{price}"

    def sell(self, symbol, qty, price):

        cost = qty * price

        self.state["balance"] += cost

        position = {
            "id": len(self.state["open_positions"]) + 1,
            "symbol": symbol,
            "side": "SELL",
            "qty": qty,
            "entry": price,
            "ltp": price,
            "pnl": 0,
            "opened_at": str(datetime.now())
        }

        self.state["open_positions"].append(position)

        self.save()

        return True, f"SELL {symbol} @ ₹{price}"

    def update_price(self, symbol, ltp):

        for pos in self.state["open_positions"]:

            if pos["symbol"] != symbol:
                continue

            pos["ltp"] = ltp

            if pos["side"] == "BUY":
                pos["pnl"] = (ltp - pos["entry"]) * pos["qty"]

            else:
                pos["pnl"] = (pos["entry"] - ltp) * pos["qty"]

        self.save()

    def close_position(self, symbol, exit_price):

        for pos in self.state["open_positions"]:

            if pos["symbol"] != symbol:
                continue

            if pos["side"] == "BUY":

                pnl = (
                    exit_price - pos["entry"]
                ) * pos["qty"]

                self.state["balance"] += (
                    exit_price * pos["qty"]
                )

            else:

                pnl = (
                    pos["entry"] - exit_price
                ) * pos["qty"]

                self.state["balance"] += pnl

            pos["exit"] = exit_price
            pos["closed_at"] = str(datetime.now())
            pos["pnl"] = pnl

            self.state["closed_positions"].append(pos)

            self.state["open_positions"].remove(pos)

            self.state["realized_pnl"] += pnl

            self.save()

            return True, pnl

        return False, "Position not found"

    def get_positions(self):
        return self.state["open_positions"]

    def get_closed(self):
        return self.state["closed_positions"]

    def summary(self):

        open_pnl = sum(
            p["pnl"]
            for p in self.state["open_positions"]
        )

        return {
            "balance": self.state["balance"],
            "equity": self.state["balance"] + open_pnl,
            "open_pnl": open_pnl,
            "realized_pnl": self.state["realized_pnl"],
            "open_positions": len(
                self.state["open_positions"]
            )
        }