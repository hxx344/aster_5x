"""MARKET quantity filters from exchangeInfo; deterministic fixtures, no HTTP."""
import copy
from fractions import Fraction
import unittest

from trading.exchange import MarketData, RateBudget
from trading.models import TradingError, dec, floor_step, plan_pair
from .helpers import Fixture


def exchange_info(market_lot=None, *, lot=None, symbol="XAUUSD1"):
    filters = [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001", **(lot or {})},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]
    if market_lot is not None:
        filters.append({"filterType": "MARKET_LOT_SIZE", **market_lot})
    return {"symbols": [{"symbol": symbol, "marginAsset": "USD1", "status": "TRADING",
                          "quantityPrecision": 0, "orderTypes": ["LIMIT", "MARKET"], "filters": filters}]}


class FixtureAPI:
    def __init__(self, response):
        self.response, self.calls, self.budget = response, [], RateBudget()

    def call(self, method, path, *args, **kwargs):
        self.calls.append((method, path))
        return copy.deepcopy(self.response)


class MarketOrderRuleTests(unittest.TestCase):
    def load(self, response):
        api = FixtureAPI(response)
        market = MarketData(api)
        market.load_rules()
        self.assertEqual(api.calls, [("GET", "/fapi/v3/exchangeInfo")])
        return market

    def test_missing_market_filter_preserves_existing_lot_tick_and_notional_rules(self):
        rule = self.load(exchange_info()).rules["XAUUSD1"]
        self.assertEqual((rule.step, rule.min_qty, rule.max_qty), (dec(".001"), dec(".001"), dec(100)))
        self.assertEqual((rule.tick, rule.min_notional, rule.margin_asset), (dec(".01"), dec(5), "USD1"))

    def test_market_specific_minimum_maximum_and_step_are_applied(self):
        data = exchange_info({"minQty": "0.010", "maxQty": "1.25", "stepSize": "0.005"})
        rule = self.load(data).rules["XAUUSD1"]
        self.assertEqual((rule.step, rule.min_qty, rule.max_qty), (dec(".005"), dec(".01"), dec("1.25")))

    def test_wider_market_filter_does_not_discard_general_lot_limits(self):
        data = exchange_info({"minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                             lot={"minQty": ".010", "maxQty": "50", "stepSize": ".005"})
        rule = self.load(data).rules["XAUUSD1"]
        self.assertEqual((rule.step, rule.min_qty, rule.max_qty), (dec(".005"), dec(".01"), dec(50)))

    def test_disabled_market_fields_retain_the_corresponding_general_lot_rule(self):
        cases = [
            ({"minQty": "0", "maxQty": "0", "stepSize": "0"}, (".001", ".001", "100")),
            ({"minQty": ".005", "maxQty": ".009", "stepSize": "0"}, (".001", ".005", ".009")),
            ({"minQty": "0", "maxQty": "1", "stepSize": ".01"}, (".01", ".01", "1")),
        ]
        for market_lot, expected in cases:
            with self.subTest(market_lot=market_lot):
                rule = self.load(exchange_info(market_lot)).rules["XAUUSD1"]
                self.assertEqual((rule.step, rule.min_qty, rule.max_qty), tuple(map(dec, expected)))

    def test_disabled_general_fields_can_use_valid_market_rules(self):
        data = exchange_info({"minQty": ".01", "maxQty": "2", "stepSize": ".005"},
                             lot={"minQty": "0", "maxQty": "0", "stepSize": "0"})
        rule = self.load(data).rules["XAUUSD1"]
        self.assertEqual((rule.step, rule.min_qty, rule.max_qty), (dec(".005"), dec(".01"), dec(2)))

    def test_non_dividing_steps_use_exact_common_multiples(self):
        data = exchange_info({"minQty": ".003", "maxQty": "1", "stepSize": ".003"},
                             lot={"minQty": ".002", "stepSize": ".002"})
        rule = self.load(data).rules["XAUUSD1"]
        self.assertEqual(rule.step, dec(".006"))
        quantity = floor_step(dec(".017"), rule.step)
        self.assertEqual(quantity, dec(".012"))
        for minimum, step in ((".002", ".002"), (".003", ".003")):
            self.assertEqual((Fraction(quantity) - Fraction(dec(minimum))) % Fraction(dec(step)), 0)

    def test_high_precision_step_intersection_has_no_decimal_rounding(self):
        data = exchange_info({"minQty": "0", "maxQty": "1", "stepSize": "3e-40"},
                             lot={"minQty": "0", "stepSize": "2e-40"})
        rule = self.load(data).rules["XAUUSD1"]
        self.assertEqual(rule.step, dec("6e-40"))
        self.assertEqual(rule.min_qty, dec("6e-40"))

    def test_missing_or_invalid_market_fields_are_not_silently_ignored(self):
        for field in ("minQty", "maxQty", "stepSize"):
            for value in (None, True, "-1", "NaN", "Infinity"):
                market_lot = {"minQty": ".001", "maxQty": "1", "stepSize": ".001", field: value}
                with self.subTest(field=field, value=value), self.assertRaises(TradingError):
                    self.load(exchange_info(market_lot))
            market_lot = {"minQty": ".001", "maxQty": "1", "stepSize": ".001"}
            del market_lot[field]
            with self.subTest(field=field, value="missing"), self.assertRaises(TradingError):
                self.load(exchange_info(market_lot))

    def test_no_positive_step_or_upper_limit_fails_without_using_quantity_precision(self):
        for field in ("stepSize", "maxQty"):
            data = exchange_info({"minQty": "0", "maxQty": "1", "stepSize": ".001", field: "0"}, lot={field: "0"})
            data["symbols"][0]["quantityPrecision"] = 8
            with self.subTest(field=field), self.assertRaisesRegex(TradingError, "有效步长或数量上限"):
                self.load(data)

    def test_offset_quantity_grids_are_rejected_instead_of_generating_invalid_orders(self):
        for source in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            data = exchange_info({"minQty": ".001", "maxQty": "1", "stepSize": ".001"})
            for constraint in data["symbols"][0]["filters"]:
                if constraint["filterType"] == source:
                    constraint.update(minQty=".0015", stepSize=".001")
            with self.subTest(source=source), self.assertRaisesRegex(TradingError, "下限与步长不对齐"):
                self.load(data)

    def test_empty_intersection_is_rejected_even_when_each_filter_has_a_quantity(self):
        data = exchange_info({"minQty": ".003", "maxQty": ".005", "stepSize": ".003"},
                             lot={"minQty": ".002", "maxQty": ".005", "stepSize": ".002"})
        with self.assertRaisesRegex(TradingError, "没有有效步进"):
            self.load(data)

    def test_duplicate_market_filters_do_not_choose_the_most_permissive_one(self):
        data = exchange_info({"minQty": ".001", "maxQty": "1", "stepSize": ".001"})
        data["symbols"][0]["filters"].append({"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "0", "stepSize": "0"})
        with self.assertRaisesRegex(TradingError, "过滤器重复"):
            self.load(data)

    def test_failed_rule_refresh_does_not_replace_last_complete_rule_set(self):
        market = self.load(exchange_info())
        previous_rules, previous_assets = market.rules, market.assets
        market.api.response = exchange_info({"minQty": ".001", "maxQty": "1", "stepSize": "-1"})
        with self.assertRaises(TradingError):
            market.load_rules()
        self.assertIs(market.rules, previous_rules)
        self.assertIs(market.assets, previous_assets)

    def test_symbols_without_market_order_support_are_not_exposed_for_entry(self):
        for field in ("orderTypes", "OrderType"):
            data = exchange_info()
            data["symbols"][0].pop("orderTypes")
            data["symbols"][0][field] = ["LIMIT"]
            with self.subTest(field=field):
                market = self.load(data)
                self.assertEqual(market.rules, {})
                self.assertEqual(market.assets, {"XAUUSD1": "USD1"})

    def test_unrelated_market_filters_do_not_block_strategy_rules_or_lose_asset_mapping(self):
        data = exchange_info()
        unrelated = exchange_info({"minQty": ".0015", "maxQty": "1", "stepSize": ".001"}, symbol="BTCUSD1")
        data["symbols"].extend(unrelated["symbols"])
        market = self.load(data)
        self.assertEqual(set(market.rules), {"XAUUSD1"})
        self.assertEqual(market.assets, {"XAUUSD1": "USD1", "BTCUSD1": "USD1"})

    def test_missing_order_type_listing_and_notional_alias_remain_compatible(self):
        data = exchange_info()
        data["symbols"][0].pop("orderTypes")
        data["symbols"][0]["filters"][-1] = {"filterType": "NOTIONAL", "minNotional": "5"}
        self.assertEqual(self.load(data).rules["XAUUSD1"].min_notional, dec(5))
        data["symbols"][0]["filters"][-1] = {"filterType": "MIN_NOTIONAL", "notional": "0"}
        self.assertEqual(self.load(data).rules["XAUUSD1"].min_notional, 0)

    def test_market_plan_honors_effective_step_and_maximum_quantity(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        rule = self.load(exchange_info({"minQty": ".003", "maxQty": ".200", "stepSize": ".003"},
                                       lot={"minQty": ".002", "stepSize": ".002"})).rules["XAUUSD1"]
        snapshot = fixture.broker.snapshot(["XAUUSD1"])
        book = fixture.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, rule, {4: dec("500000")}, fixture.account["policy"])
        self.assertEqual(plan.qty, dec(".198"))
        self.assertLessEqual(plan.qty, dec(".200"))
        self.assertEqual(plan.qty % dec(".002"), 0)
        self.assertEqual(plan.qty % dec(".003"), 0)


if __name__ == "__main__":
    unittest.main()
