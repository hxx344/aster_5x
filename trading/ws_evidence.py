"""Bounded private-event evidence, never an account snapshot or order query.

The broker owns synchronization with its snapshot lock. A missing, stale or
contradictory field only disables the shortcut; REST remains authoritative.
"""
from collections import OrderedDict
from fractions import Fraction
import math
import re
import time
import uuid

from .models import TradingError, dec, positive, wire


TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"})
STATUSES = TERMINAL | {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}
TRANSFER_REASONS = frozenset({"ASSET_TRANSFER", "MARGIN_TRANSFER"})


def _integer(value, maximum=253402300799999):
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError("Invalid event integer")
    return value


def _text(value, maximum=128):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1," + str(maximum) + r"}", value):
        raise ValueError("Invalid event identity")
    return value


class PrivateEventEvidence:
    MAX_ORDERS = 256
    MAX_AGE = 30
    FUTURE_TOLERANCE = 1

    def __init__(self):
        self.reset(False)

    def reset(self, connected):
        self.connected = connected is True
        self.session = uuid.uuid4().hex if self.connected else None
        self.cursor = 0
        self.kind = None
        self.orders = OrderedDict()
        self.balance = None
        self.balance_watermark = None

    def event(self, kind):
        self.cursor += 1
        self.kind = kind
        # Any later account event revokes a transfer completion guard.
        self.balance = None

    def _fresh(self, stamps, received_at=None):
        now = time.time()
        if not all(now - self.MAX_AGE <= stamp / 1000 <= now + self.FUTURE_TOLERANCE for stamp in stamps):
            return False
        return received_at is None or 0 <= time.monotonic() - received_at <= self.MAX_AGE

    def payload(self, event):
        if not self.connected or not isinstance(event, dict) or event.get("e") != self.kind:
            return
        if self.kind == "ORDER_TRADE_UPDATE":
            self._order(event)
        elif self.kind == "ACCOUNT_UPDATE":
            self._balance(event)

    def _remember(self, key, value):
        self.orders[key] = value
        self.orders.move_to_end(key)
        while len(self.orders) > self.MAX_ORDERS:
            self.orders.popitem(last=False)

    def _order(self, event):
        row = event.get("o")
        try:
            key = (_text(row["s"], 40), _text(row["c"]))
        except (KeyError, TypeError, ValueError):
            # An unidentifiable order event cannot leave prior order evidence
            # trusted: it may be a correction to any cached receipt.
            for known in self.orders:
                self.orders[known] = None
            return
        previous = self.orders.get(key)
        if key in self.orders and previous is None:
            return
        try:
            stamps = tuple(_integer(value) for value in (event["E"], event["T"], row["T"]))
            if not self._fresh(stamps):
                raise ValueError
            # Outer transaction time can precede the inner trade time. All
            # three clocks must remain fresh and progress independently.
            qty = positive(row["q"])
            executed = positive(row["z"], True)
            average = positive(row["ap"], True)
            status = row["X"]
            if (status not in STATUSES or row["S"] not in ("BUY", "SELL")
                    or row["ps"] not in ("LONG", "SHORT", "BOTH")
                    or executed > qty or (executed and not average)
                    or (status == "FILLED" and executed != qty)
                    or (status in ("NEW", "REJECTED") and executed)
                    or (status == "PARTIALLY_FILLED" and not 0 < executed < qty)):
                raise ValueError
            original_type = _text(row["ot"], 40)
            order_type = _text(row["o"], 40)
            receipt = {"symbol": key[0], "clientOrderId": key[1], "side": row["S"],
                "positionSide": row["ps"], "type": order_type, "origType": original_type,
                "origQty": wire(qty), "executedQty": wire(executed), "avgPrice": wire(average),
                "status": status, "orderId": _integer(row["i"], 2**63 - 1), "updateTime": max(stamps[1:])}
            if previous is not None:
                old = previous["receipt"]
                # Retransmission is idempotent, but never renews its original
                # freshness window. Equal timestamps with changed data fail.
                if receipt == old and stamps == previous["stamps"]:
                    return
                identity = ("symbol", "clientOrderId", "side", "positionSide", "type", "origType", "origQty", "orderId")
                if (any(receipt[field] != old[field] for field in identity)
                        or any(new < old_time for new, old_time in zip(stamps, previous["stamps"]))
                        or stamps[0] == previous["stamps"][0]
                        or executed < dec(old["executedQty"])
                        or old["status"] in TERMINAL
                        or (old["status"] == "PARTIALLY_FILLED" and status == "NEW")
                        or (executed == dec(old["executedQty"]) and average != dec(old["avgPrice"]))):
                    raise ValueError
            self._remember(key, {"receipt": receipt, "stamps": stamps, "received_at": time.monotonic()})
        except (KeyError, TypeError, ValueError, ArithmeticError, TradingError):
            # Keep a bounded tombstone so a later valid-looking event cannot
            # resurrect an already contradictory order within this session.
            self._remember(key, None)

    def order_receipt(self, order):
        if not self.connected or not isinstance(order, dict):
            return None
        try:
            item = self.orders.get((order["symbol"], order["newClientOrderId"]))
            if item is None or not self._fresh(item["stamps"], item["received_at"]):
                return None
            receipt = item["receipt"]
            if (receipt["status"] not in TERMINAL
                    or any(receipt[field] != order[field] for field in ("symbol", "side", "positionSide"))
                    or receipt["origType"] != order["type"] or receipt["type"] != order["type"]
                    or dec(receipt["origQty"]) != positive(order["quantity"])):
                return None
            return dict(receipt)
        except (KeyError, TypeError, ValueError, TradingError):
            return None

    def checkpoint(self, wallet):
        if not self.connected:
            return None
        try:
            wallet = wire(positive(wallet, True))
        except (TradingError, ValueError, TypeError):
            return None
        return {"stream_session": self.session, "event_cursor": self.cursor, "wallet": wallet}

    def checkpoint_current(self, checkpoint):
        if (not self.connected or not isinstance(checkpoint, dict)
                or checkpoint.get("stream_session") != self.session
                or type(checkpoint.get("event_cursor")) is not int
                or not 0 <= checkpoint["event_cursor"] <= self.cursor):
            return False
        try:
            positive(checkpoint.get("wallet"), True)
            return True
        except TradingError:
            return False

    def _balance(self, event):
        try:
            stamps = (_integer(event["E"]), _integer(event["T"]))
            if not self._fresh(stamps):
                return
            previous = self.balance_watermark
            # Equality is a duplicate or a conflict; neither can authorize.
            if previous is not None and (any(a < b for a, b in zip(stamps, previous)) or stamps == previous):
                return
            self.balance_watermark = stamps
            account = event["a"]
            if account["m"] not in TRANSFER_REASONS or not isinstance(account["B"], list):
                return
            if any(not isinstance(row, dict) for row in account["B"]):
                return
            balances = [row for row in account["B"] if row.get("a") == "USD1"]
            if len(balances) != 1:
                return
            row = balances[0]
            self.balance = {"source": "websocket", "stream_session": self.session,
                "event_cursor": self.cursor, "event_time": stamps[0] / 1000,
                "transaction_time": stamps[1] / 1000, "reason": account["m"], "asset": "USD1",
                "delta": wire(dec(row["bc"])), "wallet": wire(positive(row["wb"], True)),
                "_stamps": stamps, "_received_at": time.monotonic()}
        except (KeyError, TypeError, ValueError, ArithmeticError, TradingError):
            return

    def transfer_balance(self, checkpoint, delta, created_at):
        if not self.checkpoint_current(checkpoint):
            return None
        try:
            event = self.balance
            if (event is None or event["event_cursor"] <= checkpoint["event_cursor"]
                    or event["event_cursor"] != self.cursor
                    or type(created_at) not in (int, float) or not math.isfinite(created_at) or created_at <= 0
                    or event["_stamps"][1] < created_at * 1000
                    or not self._fresh(event["_stamps"], event["_received_at"])):
                return None
            amount = Fraction(dec(wire(delta)))
            if (not amount or Fraction(dec(event["delta"])) != amount
                    or Fraction(dec(event["wallet"])) != Fraction(dec(checkpoint["wallet"])) + amount):
                return None
            return {key: value for key, value in event.items() if not key.startswith("_")}
        except (KeyError, TypeError, ValueError, ArithmeticError, TradingError):
            return None
