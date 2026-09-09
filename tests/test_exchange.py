import copy
import unittest
from urllib.parse import parse_qsl, urlencode
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_typed_data
import httpx

from trading.exchange import API, AmbiguousOrder, ExchangeError, LiveBroker, RateBudget, credentials_for
from trading.models import TradingError, dec
from trading.paper import DemoMarket, PAPER_BRACKETS


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        # Public deterministic signing fixture, never a funded or user-supplied key.
        key = bytes(range(1, 33))
        self.credentials = {"private_key": key, "signer": Account.from_key(key).address, "user": "0x" + "22" * 20}

    def test_signature_covers_exact_wire_parameters_and_nonce_is_monotonic(self):
        api = API(self.credentials)
        self.addCleanup(api.close)
        with patch("time.time_ns", return_value=123456789000):
            first = api.signed_parameters({"symbol": "XAUUSD1", "quantity": "0.001"})
            second = api.signed_parameters({})
        signature = first.pop("signature")
        message = encode_typed_data(domain_data={"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
            "verifyingContract": "0x" + "00" * 20}, message_types={"Message": [{"name": "msg", "type": "string"}]},
            message_data={"msg": urlencode(first)})
        self.assertEqual(Account.recover_message(message, signature=signature), self.credentials["signer"])
        self.assertGreater(int(second["nonce"]), int(first["nonce"]))

    def test_post_is_form_encoded_and_server_errors_are_ambiguous(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(503, text="secret remote details")
        api = API(self.credentials, transport=httpx.MockTransport(handle), budget=RateBudget())
        self.addCleanup(api.close)
        with self.assertRaises(AmbiguousOrder) as caught:
            api.call("POST", "/fapi/v3/order", {"symbol": "XAUUSD1"}, signed=True)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(seen[0].url.query, b"")
        self.assertIn("signature", dict(parse_qsl(seen[0].content.decode())))

    def test_rate_limit_stops_following_requests(self):
        count = []
        api = API(transport=httpx.MockTransport(lambda r: count.append(r) or httpx.Response(429, headers={"Retry-After": "250"})), budget=RateBudget())
        self.addCleanup(api.close)
        for _ in range(2):
            with self.assertRaises(ExchangeError):
                api.call("GET", "/fapi/v3/time")
        self.assertEqual(len(count), 1)

    def test_invalid_credentials_are_sanitized(self):
        with patch.dict("os.environ", {"ASTER_TEST_USER": self.credentials["user"], "ASTER_TEST_SIGNER": self.credentials["signer"], "ASTER_TEST_PRIVATE_KEY": "secret_invalid_key"}):
            with self.assertRaises(TradingError) as caught:
                credentials_for("ASTER_TEST")
        self.assertNotIn("secret_invalid_key", str(caught.exception))

    def test_snapshot_uses_usd1_cross_balances_not_usdt_totals(self):
        positions = [{"symbol": "XAUUSD1", "positionSide": s, "positionAmt": "0", "entryPrice": "0", "markPrice": "100",
                      "leverage": "4", "unRealizedProfit": "0", "liquidationPrice": "0", "marginType": "cross"} for s in ("LONG", "SHORT")]
        account_positions = [{**p, "isolated": False} for p in positions]
        responses = {
            "/fapi/v3/positionSide/dual": {"dualSidePosition": True}, "/fapi/v3/multiAssetsMargin": {"multiAssetsMargin": False},
            "/fapi/v3/accountWithJoinMargin": {"canTrade": True, "totalMarginBalance": "99000", "totalMaintMargin": "9999",
                "assets": [{"asset": "USD1", "crossWalletBalance": "210", "crossUnPnl": "-10", "maintMargin": "13", "availableBalance": "123"}], "positions": account_positions},
            "/fapi/v3/positionRisk": positions, "/fapi/v3/openOrders": [],
            "/fapi/v3/leverageBracket": {"symbol": "XAUUSD1", "brackets": PAPER_BRACKETS},
            "/fapi/v3/commissionRate": {"takerCommissionRate": "0.0004"},
        }
        class FakeAPI:
            def call(self, method, path, *args, **kwargs):
                return copy.deepcopy(responses[path])
        broker = LiveBroker(self.credentials, DemoMarket(), api=FakeAPI())
        snapshot = broker.snapshot(["XAUUSD1"])
        self.assertEqual(snapshot.equity, 200)
        self.assertEqual(snapshot.maintenance, 13)
        self.assertEqual(snapshot.occupied_margin, 0)
        self.assertEqual(snapshot.ratio, 0)
        self.assertEqual(snapshot.available, 123)
        extra = {**positions[0], "symbol": "SPCXUSD1", "positionAmt": "1", "entryPrice": "100", "leverage": "10", "isolated": False}
        account_positions.append(extra)
        with self.assertRaisesRegex(TradingError, "全部持仓尚未同步"):
            broker.snapshot(["XAUUSD1"])
        positions.append({**extra, "positionAmt": "0"})
        with self.assertRaisesRegex(TradingError, "全部持仓尚未同步"):
            broker.snapshot(["XAUUSD1"])
        positions[-1]["positionAmt"] = "1"
        self.assertEqual(broker.snapshot(["XAUUSD1"]).occupied_margin, 10)
        positions.pop()
        account_positions.pop()
        account_positions[0]["isolated"] = True
        with self.assertRaisesRegex(TradingError, "保证金模式尚未同步"):
            broker.snapshot(["XAUUSD1"])
        account_positions[0]["isolated"] = False
        responses["/fapi/v3/positionSide/dual"]["dualSidePosition"] = False
        responses["/fapi/v3/multiAssetsMargin"]["multiAssetsMargin"] = True
        changed = broker.snapshot(["XAUUSD1"], fresh_modes=True)
        self.assertFalse(changed.hedge_mode)
        self.assertTrue(changed.multi_assets)
        # V3 may omit empty positions; authenticated account rows still supply 4x.
        responses["/fapi/v3/positionRisk"] = []
        self.assertEqual(broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].leverage, 4)
        responses["/fapi/v3/accountWithJoinMargin"]["positions"][0]["positionAmt"] = "1"
        with self.assertRaises(TradingError):
            broker.snapshot(["XAUUSD1"])
