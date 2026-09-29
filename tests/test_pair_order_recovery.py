"""Manual missing-order recovery uses paper ledgers and offline GET fixtures only."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import sqlite3
import time
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests import test_pair_trading as trading_tests
from tests.test_cycle_account_snapshot import ACCOUNT, ORDERS, RISK, risk_row
from trading.exchange import AmbiguousOrder, ExchangeError, LiveBroker, RateBudget, RequestNotSent
from trading.execution import Executor
from trading.models import TradingError, dec, wire
from trading.pair_execution import PairTrader, empty_progress, runtime_default
from trading.store import Store


SYMBOL = "XAUUSD1"
BATCH_ID = "e38af3e9e37845fca5072e21432700000"


def seed_pending(engine, before=None):
    """Seed a submitted, unacknowledged ordinary batch without submitting orders."""
    store = engine.store
    pair = store.pair("gold")
    if pair is None:
        pair = engine.pairs.create(integration.pair_config(
            ordinary={"enabled": True}, cycle={"enabled": False}))
    before = dict(before or {"LONG": "3.039", "SHORT": "3.039"})
    _, brokers, identities = PairTrader(engine)._members(pair)
    for key, side in (("long", "LONG"), ("short", "SHORT")):
        broker = brokers[key]
        if isinstance(broker, LiveBroker):
            rows = broker.api.responses[ACCOUNT]["positions"]
            for row in rows:
                if row["symbol"] == SYMBOL and row["positionSide"] == side:
                    row.update(positionAmt=before[side] if side == "LONG" else "-" + before[side], entryPrice="100")
            broker.api.responses[RISK] = [risk_row(row) for row in rows]
            broker.api.responses["/fapi/v3/order"] = ExchangeError("order not found", code=-2013)
            broker.api.responses["/fapi/v3/allOrders"] = []
        else:
            broker.state["positions"][SYMBOL + ":" + side].update(qty=before[side], entry="4412.015")
            broker.save()
    legs = [{"key": key, "order": Executor.order(SYMBOL, side, direction, dec("0.2"), side[0] + BATCH_ID[:28]),
             "dispatch": "sending", "receipt": None, "error": "previous result unknown"}
            for key, side, direction in (("long", "LONG", "BUY"), ("short", "SHORT", "SELL"))]
    pending = {"id": BATCH_ID, "kind": "ordinary", "phase": "open", "symbol": SYMBOL,
               "identities": identities, "created_at": time.time() - 3600, "quantity": "0.2",
               "before": before, "target": {side: wire(dec(qty) + dec("0.2")) for side, qty in before.items()},
               "leverage": 5, "legs": legs, "repairs": [], "repair_attempts": 0,
               "config": {**pair["cycle"], "leverage": 5}}
    runtime = {"phase": "reconciling", "reason": "原订单结果未知", "owned": before,
               "identities": identities, "progress": empty_progress(before, 4), "pending": pending,
               "daily_volume": {"2026-09-29": {"long": "123", "short": "456"}},
               "last_batch": {"id": "completed-before", "completed": True}, "updated_at": time.time()}
    store.put("pair_runtime:gold", runtime)
    store.put("pair_batch:completed-before", {"id": "completed-before", "completed": True})
    return deepcopy(runtime)


def receipt_for(leg, *, status="EXPIRED", qty="0", client_id=None, timestamp=None):
    order = leg["order"]
    cid = client_id or order["newClientOrderId"]
    return {"symbol": order["symbol"], "clientOrderId": cid, "positionSide": order["positionSide"],
            "side": order["side"], "type": "MARKET", "origType": "MARKET", "status": status,
            "executedQty": qty, "origQty": order["quantity"], "avgPrice": "4412.015" if dec(qty) else "0",
            "orderId": "paper:" + cid, "time": timestamp or int((time.time() - 3500) * 1000),
            "updateTime": timestamp or int((time.time() - 3500) * 1000)}


class PairOrderRecoveryTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp

    def seed(self, before=None):
        return seed_pending(self.engine, before)

    def state(self):
        return self.f.store.get("pair_runtime:gold")

    def broker(self, side="long"):
        pair = self.f.store.pair("gold")
        return self.engine.broker(self.f.store.account(pair[side + "_account_id"]))

    def no_writes(self):
        stack = ExitStack()
        for key in ("long", "short"):
            broker = self.broker(key)
            for method in ("submit", "set_leverage", "set_cycle_leverage"):
                stack.enter_context(patch.object(broker, method, side_effect=AssertionError("recovery must not trade")))
        stack.enter_context(patch("trading.margin_balance.MarginBalancer.tick", side_effect=AssertionError("recovery must not transfer")))
        return stack

    def preview(self):
        return self.engine.pairs.preview_recovery("gold")

    def confirm(self, token, acknowledge=True):
        return self.engine.pairs.confirm_recovery("gold", token, acknowledge_unknown=acknowledge)

    def audit(self):
        return self.f.store.get("pair_order_recovery:gold:" + BATCH_ID)

    def event_rows(self):
        with self.f.store.connect() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY id")]

    def change_state(self, mutate):
        state = self.state()
        mutate(state)
        self.f.store.put("pair_runtime:gold", state)
        return state

    def test_archive_retains_original_unknown_batch_and_accounting_without_writes(self):
        original = self.seed()
        history = self.f.store.get("pair_batch:completed-before")
        with self.no_writes():
            review = self.preview()
            self.assertEqual(review["status"], "review")
            self.assertEqual(review["before"], original["owned"])
            self.assertEqual(review["actual"], original["owned"])
            self.assertEqual({row["result"] for row in review["orders"]}, {"not_found"})
            self.assertEqual({row["client_order_id"] for row in review["orders"]},
                             {row["order"]["newClientOrderId"] for row in original["pending"]["legs"]})
            self.assertIsNotNone(self.state()["pending"])
            self.confirm(review["token"])
        state = self.state()
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertIsNone(state["pending"])
        self.assertEqual(state["owned"], original["owned"])
        self.assertEqual(state["progress"]["baseline"], original["owned"])
        self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(state["progress"]["completed_cycles"], 4)
        self.assertEqual(state["daily_volume"], original["daily_volume"])
        self.assertEqual(state["last_batch"], original["last_batch"])
        self.assertEqual(self.f.store.get("pair_batch:completed-before"), history)
        self.assertIsNone(self.f.store.get("pair_batch:" + BATCH_ID))
        audit = self.audit()
        self.assertIsNotNone(audit)
        self.assertIn("manual_archived_unresolved", str(audit))
        self.assertIn(original["pending"], audit.values())
        self.assertTrue(state["recovery_watch"])

    def test_each_side_may_have_a_different_nonzero_matching_baseline(self):
        before = {"LONG": "0.4", "SHORT": "0.7"}
        self.seed(before)
        with self.no_writes():
            self.confirm(self.preview()["token"])
        self.assertEqual(self.state()["owned"], before)

    def test_equal_actual_positions_that_differ_from_before_are_rejected(self):
        self.seed()
        for key, side in (("long", "LONG"), ("short", "SHORT")):
            broker = self.broker(key)
            broker.state["positions"][SYMBOL + ":" + side]["qty"] = "3.239"
            broker.save()
        with self.no_writes(), self.assertRaises(TradingError):
            self.preview()
        self.assertIsNotNone(self.state()["pending"])
        self.assertIsNone(self.audit())

    def test_unsafe_pending_shapes_and_tracking_block_preview(self):
        original = self.seed()
        mutations = {
            "young": lambda s: s["pending"].update(created_at=time.time() - 30),
            "too_old": lambda s: s["pending"].update(created_at=time.time() - 7 * 86400),
            "future": lambda s: s["pending"].update(created_at=time.time() + 10),
            "cycle": lambda s: s["pending"].update(kind="cycle"),
            "close": lambda s: s["pending"].update(phase="close"),
            "repairs": lambda s: s["pending"].update(repairs=[deepcopy(s["pending"]["legs"][0])]),
            "cycle_increment": lambda s: s["progress"]["quantities"].update(LONG="0.1"),
            "wrong_owned": lambda s: s.update(owned={"LONG": "1", "SHORT": "1"}),
            "wrong_identity": lambda s: s["pending"]["identities"]["long"].update(env_prefix="ASTER_REPLACED"),
        }
        for name, mutate in mutations.items():
            with self.subTest(boundary=name):
                self.f.store.put("pair_runtime:gold", original)
                changed = self.change_state(mutate)
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), changed)
                self.assertIsNone(self.audit())

    def test_running_group_and_each_pending_transfer_state_are_rejected(self):
        self.seed()
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "enabled": True})
        with self.assertRaises(TradingError):
            self.preview()
        self.f.store.save_pair({**self.f.store.pair("gold"), "enabled": False})
        for margin in ({"pending": {}}, {"pending": {"id": "transfer"}},
                       *({"status": status} for status in ("submitting", "accepted", "acknowledged", "unknown"))):
            with self.subTest(margin=margin):
                self.f.store.put("pair_margin:gold", margin)
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()

    def test_network_budget_and_other_exchange_errors_do_not_prove_absence(self):
        original = self.seed()
        for error in (ExchangeError("network failure"), ExchangeError("rate limited", code=-1003),
                      ExchangeError("wrong account", code=-2015), RequestNotSent("budget unavailable", retry_after=30)):
            with self.subTest(error=str(error)), patch.object(self.broker(), "query", side_effect=error):
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), original)

    def test_invalid_or_unmatched_receipt_cannot_clear_the_pending_batch(self):
        original = self.seed()
        row = receipt_for(original["pending"]["legs"][0])
        for changed in ({**row, "clientOrderId": "another-client"}, {**row, "symbol": "CLUSD1"},
                        {**row, "positionSide": "SHORT"}, {**row, "origQty": "0.3"},
                        {**row, "status": "FILLED", "executedQty": "0.1", "avgPrice": "4000"}):
            with self.subTest(receipt=changed), patch.object(self.broker(), "query", return_value=changed):
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), original)

    def test_matching_history_receipt_is_persisted_without_synthetic_completion(self):
        original = self.seed()
        row = receipt_for(original["pending"]["legs"][0], status="FILLED", qty="0.2")
        broker = self.broker()
        broker.state["orders"][row["clientOrderId"]] = row
        broker.save()
        with self.no_writes(), patch.object(broker, "query", side_effect=ExchangeError("not found", code=-2013)):
            result = self.preview()
        self.assertEqual(result["status"], "receipts_found")
        self.assertNotIn("token", result)
        self.assertEqual(self.state()["pending"]["legs"][0]["receipt"], row)
        self.assertIsNone(self.state()["pending"]["legs"][1]["receipt"])
        self.assertEqual(self.state()["owned"], original["owned"])
        self.assertIsNone(self.audit())

    def test_full_or_malformed_order_history_fails_closed(self):
        original = self.seed()
        broker = self.broker()
        orders = {"unrelated-" + str(i): receipt_for(original["pending"]["legs"][0], client_id="unrelated-" + str(i))
                  for i in range(1000)}
        for rows in (orders, {"bad-row": None}, {"bad-row": {"clientOrderId": "unknown"}}):
            with self.subTest(history_size=len(rows)):
                broker.state["orders"] = rows
                broker.save()
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), original)

    def test_receipt_appearing_between_preview_and_confirm_prevents_archive(self):
        original = self.seed()
        review = self.preview()
        row = receipt_for(original["pending"]["legs"][0], status="FILLED", qty="0.2")
        broker = self.broker()
        broker.state["orders"][row["clientOrderId"]] = row
        broker.save()
        with self.no_writes(), self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertIsNotNone(self.state()["pending"])
        self.assertIsNone(self.audit())

    def test_background_poll_diagnostics_do_not_invalidate_confirmation(self):
        self.seed()
        review = self.preview()
        def poll(state):
            state.update(updated_at=time.time() + 1, reason="updated query explanation", snapshots={"long": {"timestamp": 1}})
            state["pending"]["observation_attempt_at"] = time.time()
            for leg in state["pending"]["legs"]:
                leg["error"] = "Aster code -2013"
        self.change_state(poll)
        with self.no_writes():
            self.confirm(review["token"])
        self.assertIsNone(self.state()["pending"])

    def test_real_background_observation_keeps_existing_archive_preview_valid(self):
        self.seed()
        with self.no_writes() as stack:
            for key in ("long", "short"):
                stack.enter_context(patch.object(self.broker(key), "query", side_effect=
                    ExchangeError("Order does not exist", code=-2013)))
            self.engine.pairs.tick("gold")
            review = self.preview()
            previous = self.state()["pending"]["observation_attempt_at"]
            self.change_state(lambda state: state["pending"].update(observation_attempt_at=previous - 4))
            self.engine.pairs.tick("gold")
            self.assertGreater(self.state()["pending"]["observation_attempt_at"], previous)
            self.assertIsNotNone(self.state()["pending"])
            self.confirm(review["token"])
        self.assertIsNone(self.state()["pending"])
        self.assertIsNotNone(self.state()["recovery_watch"])

    def test_changed_batch_or_actual_positions_invalidates_confirmation(self):
        original = self.seed()
        for changed in ("batch", "position", "settings"):
            with self.subTest(change=changed):
                self.f.store.put("pair_runtime:gold", original)
                self.broker().state["positions"][SYMBOL + ":LONG"]["qty"] = original["owned"]["LONG"]
                self.broker().save()
                review = self.preview()
                if changed == "batch":
                    self.change_state(lambda s: s["pending"].update(id="different-batch"))
                elif changed == "position":
                    self.broker().state["positions"][SYMBOL + ":LONG"]["qty"] = "3.04"
                    self.broker().save()
                else:
                    pair = self.f.store.pair("gold")
                    self.f.store.save_pair({**pair, "name": "changed name"})
                with self.no_writes(), self.assertRaises(TradingError):
                    self.confirm(review["token"])
                self.assertIsNotNone(self.state()["pending"])
                self.assertIsNone(self.audit())

    def test_acknowledgment_is_required_and_token_expiry_and_replay_are_safe(self):
        self.seed()
        for acknowledge in (False, "true", 1, None):
            with self.subTest(acknowledge=acknowledge):
                review = self.preview()
                with self.assertRaises(TradingError):
                    self.confirm(review["token"], acknowledge)
                self.assertIsNotNone(self.state()["pending"])
        review = self.preview()
        with patch("trading.pair_recovery.time.monotonic", return_value=time.monotonic() + 301), self.assertRaises(TradingError):
            self.confirm(review["token"])
        review = self.preview()
        self.confirm(review["token"])
        after, events, audit = self.state(), self.event_rows(), self.audit()
        with self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), after)
        self.assertEqual(self.event_rows(), events)
        self.assertEqual(self.audit(), audit)

    def test_account_modes_permissions_open_orders_and_original_leverage_are_required(self):
        self.seed()
        broker = self.broker()
        snapshot = broker.snapshot
        changes = {
            "hedge": lambda snap: replace(snap, hedge_mode=False),
            "assets": lambda snap: replace(snap, multi_assets=True),
            "permission": lambda snap: replace(snap, can_trade=False),
            "orders": lambda snap: replace(snap, open_orders=[{"orderId": "external"}]),
            "unknown_orders": lambda snap: replace(snap, open_orders=None),
            "leverage": lambda snap: replace(snap, positions=[replace(p, leverage=10) if p.symbol == SYMBOL else p for p in snap.positions]),
            "opposite": lambda snap: replace(snap, positions=[replace(p, qty=dec("0.1")) if p.symbol == SYMBOL and p.side == "SHORT" else p for p in snap.positions]),
        }
        for name, transform in changes.items():
            with self.subTest(mode=name), patch.object(broker, "snapshot", side_effect=lambda *a, **kw: transform(snapshot(*a, **kw))):
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()

    def test_both_leverages_changed_together_still_cannot_archive(self):
        self.seed()
        for key in ("long", "short"):
            broker = self.broker(key)
            broker.state["leverages"][SYMBOL] = 10
            broker.save()
        with self.no_writes(), self.assertRaises(TradingError):
            self.preview()

    def test_other_store_mutation_during_confirmation_is_not_overwritten(self):
        self.seed()
        review = self.preview()
        other = Store(self.f.store.path)
        original_commit = self.f.store.commit_pair_recovery
        injected = {**self.state(), "concurrent_revision": "keep-me"}
        def race(*args, **kwargs):
            other.put("pair_runtime:gold", injected)
            return original_commit(*args, **kwargs)
        with self.no_writes(), patch.object(self.f.store, "commit_pair_recovery", side_effect=race), self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), injected)
        self.assertIsNone(self.audit())

    def test_other_store_margin_account_and_membership_changes_block_commit(self):
        original = self.seed()
        other = Store(self.f.store.path)
        saved_account = other.account("test")
        commit = self.f.store.commit_pair_recovery
        for mutation in ("margin", "account", "membership"):
            with self.subTest(mutation=mutation):
                other.put("pair_margin:gold", {})
                other.save_account(saved_account)
                with other.connect() as db:
                    db.execute("UPDATE pair_members SET direction='LONG' WHERE account_id='test'")
                review = self.preview()
                def race(*args, **kwargs):
                    if mutation == "margin":
                        other.put("pair_margin:gold", {"status": "unknown", "pending": {"id": "concurrent"}})
                    elif mutation == "account":
                        other.save_account({**saved_account, "name": "changed during commit"})
                    else:
                        with other.connect() as db:
                            db.execute("DELETE FROM pair_members WHERE account_id='test'")
                    return commit(*args, **kwargs)
                with self.no_writes(), patch.object(self.f.store, "commit_pair_recovery", side_effect=race), self.assertRaises(TradingError):
                    self.confirm(review["token"])
                self.assertEqual(self.state(), original)
                self.assertIsNone(self.audit())

    def test_malformed_nested_records_fail_with_a_domain_error(self):
        original = self.seed()
        mutations = (
            lambda s: s["pending"].update(before=["invalid"]),
            lambda s: s["pending"].update(target=["invalid"]),
            lambda s: s.update(owned=["invalid"]),
            lambda s: s.update(progress=["invalid"]),
            lambda s: s["progress"].update(quantities=["invalid"]),
        )
        for mutation in mutations:
            self.f.store.put("pair_runtime:gold", original)
            self.change_state(mutation)
            with self.subTest(mutation=mutation), self.no_writes(), self.assertRaises(TradingError):
                self.preview()
            self.assertIsNone(self.audit())

    def test_failure_writing_audit_or_event_rolls_back_archival(self):
        original = self.seed()
        review = self.preview()
        events = self.event_rows()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER fail_recovery_event BEFORE INSERT ON events "
                       "WHEN NEW.account_id='gold' BEGIN SELECT RAISE(ABORT, 'injected recovery fault'); END")
        with self.no_writes(), self.assertRaises(sqlite3.DatabaseError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)
        self.assertIsNone(self.audit())
        self.assertEqual(self.event_rows(), events)
        self.assertFalse(self.f.store.pair("gold")["enabled"])

    def test_archived_group_cannot_adopt_late_positions_on_start(self):
        self.seed()
        self.confirm(self.preview()["token"])
        archived = self.state()
        for key, side in (("long", "LONG"), ("short", "SHORT")):
            broker = self.broker(key)
            broker.state["positions"][SYMBOL + ":" + side]["qty"] = "3.239"
            broker.save()
        with self.no_writes(), self.assertRaises(TradingError):
            self.engine.pairs.enable("gold", True)
        self.assertEqual(self.state(), archived)
        self.assertFalse(self.f.store.pair("gold")["enabled"])

    def test_archived_group_can_start_after_a_fresh_negative_query(self):
        self.seed()
        self.confirm(self.preview()["token"])
        broker = self.broker()
        with self.no_writes(), patch.object(broker, "query", wraps=broker.query) as queries:
            self.engine.pairs.enable("gold", True)
        self.assertTrue(self.f.store.pair("gold")["enabled"])
        self.assertGreaterEqual(queries.call_count, 1)

    def test_archived_order_watch_blocks_new_opening_on_late_receipt_or_query_error(self):
        original = self.seed()
        self.confirm(self.preview()["token"])
        pair = self.f.store.pair("gold")
        pair = self.f.store.save_pair({**pair, "enabled": True})
        trader = PairTrader(self.engine)
        for outcome in (receipt_for(original["pending"]["legs"][0], status="FILLED", qty="0.2"),
                        receipt_for(original["pending"]["legs"][0], status="NEW"),
                        ExchangeError("query unavailable")):
            options = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=str(outcome)), patch.object(self.broker(), "query", **options):
                with self.no_writes(), self.assertRaises(TradingError):
                    trader._config_guard(pair, original["identities"], opening=True)
        with patch.object(self.broker(), "query", return_value=receipt_for(original["pending"]["legs"][0])):
            trader._config_guard(pair, original["identities"], opening=True)

    def test_invalid_watch_legs_fail_closed_with_domain_error(self):
        from trading.pair_recovery import require_archived_orders_clear
        self.seed()
        self.confirm(self.preview()["token"])
        state = self.state()
        state["recovery_watch"]["batches"][0]["legs"] = [None, None]
        with self.assertRaises(TradingError):
            require_archived_orders_clear(self.engine, self.f.store.pair("gold"), state=state)

    def test_archived_order_watch_does_not_block_reductions_or_add_reads_without_a_watch(self):
        self.seed()
        pair = self.f.store.pair("gold")
        pair = self.f.store.save_pair({**pair, "enabled": True})
        trader = PairTrader(self.engine)
        with patch.object(self.broker(), "query", side_effect=AssertionError("no archive means no added query")):
            trader._config_guard(pair, self.state()["identities"], opening=True)
        self.f.store.save_pair({**pair, "enabled": False})
        self.confirm(self.preview()["token"])
        with patch.object(self.broker(), "query", side_effect=AssertionError("reductions must remain available")):
            trader._config_guard(self.f.store.pair("gold"), self.state()["identities"], opening=False)

    def test_late_archived_receipt_blocks_paper_transfer_before_any_wallet_change(self):
        from trading.margin_balance import MarginBalancer
        original = self.seed({"LONG": "0.1", "SHORT": "0.1"})
        self.confirm(self.preview()["token"])
        pair = self.f.store.pair("gold")
        pair["enabled"] = True
        pair["margin"]["enabled"] = True
        pair = self.f.store.save_pair(pair)
        for key, wallet in (("long", "3000"), ("short", "1000")):
            self.broker(key).state["wallet"] = wallet
            self.broker(key).save()
        snapshots = {key: self.broker(key).snapshot([SYMBOL]) for key in ("long", "short")}
        row = receipt_for(original["pending"]["legs"][0], status="FILLED", qty="0.2")
        balancer = MarginBalancer(self.engine)
        with patch.object(self.broker(), "query", return_value=row), \
                patch.object(balancer, "_paper", side_effect=AssertionError("late receipt must block the transfer")):
            result = balancer.tick(pair, snapshots)
        self.assertTrue(result["blocks_trading"])
        self.assertIsNone(result.get("pending"))
        self.assertEqual({key: self.broker(key).state["wallet"] for key in ("long", "short")},
                         {"long": "3000", "short": "1000"})

    def test_manager_cannot_delete_flat_group_with_unresolved_archived_orders(self):
        self.seed({"LONG": "0", "SHORT": "0"})
        with self.no_writes():
            self.confirm(self.preview()["token"])
            pair, runtime, audit = self.f.store.pair("gold"), self.state(), self.audit()
            with self.assertRaises(TradingError):
                self.engine.pairs.delete("gold")
        self.assertEqual(self.f.store.pair("gold"), pair)
        self.assertEqual(self.state(), runtime)
        self.assertEqual(self.audit(), audit)
        for aid in ("test", "second"):
            self.assertEqual(self.f.store.pair_for_account(aid)["id"], "gold")

    def test_store_cannot_delete_flat_group_with_unresolved_archived_orders(self):
        self.seed({"LONG": "0", "SHORT": "0"})
        with self.no_writes():
            self.confirm(self.preview()["token"])
            pair, runtime, audit = self.f.store.pair("gold"), self.state(), self.audit()
            with self.assertRaises(TradingError):
                self.f.store.delete_pair("gold")
        self.assertEqual(self.f.store.pair("gold"), pair)
        self.assertEqual(self.state(), runtime)
        self.assertEqual(self.audit(), audit)
        for aid in ("test", "second"):
            self.assertEqual(self.f.store.pair_for_account(aid)["id"], "gold")

    def test_flat_reconciliation_preserves_archive_watch_and_cannot_release_members(self):
        self.seed()
        self.confirm(self.preview()["token"])
        watch, audit = deepcopy(self.state()["recovery_watch"]), self.audit()
        for key, side in (("long", "LONG"), ("short", "SHORT")):
            broker = self.broker(key)
            broker.state["positions"][SYMBOL + ":" + side]["qty"] = "0"
            broker.save()
        with self.no_writes():
            self.engine.pairs.reconcile_flat("gold")
            self.assertEqual(self.state()["owned"], {"LONG": "0", "SHORT": "0"})
            self.assertEqual(self.state()["recovery_watch"], watch)
            with self.assertRaises(TradingError):
                self.engine.pairs.delete("gold")
        self.assertEqual(self.state()["recovery_watch"], watch)
        self.assertEqual(self.audit(), audit)
        for aid in ("test", "second"):
            self.assertEqual(self.f.store.pair_for_account(aid)["id"], "gold")

    def test_flat_groups_without_archive_watch_remain_deletable(self):
        for layer in ("manager", "store"):
            with self.subTest(layer=layer):
                pair_id = "normal_" + layer
                self.engine.pairs.create(integration.pair_config(id=pair_id))
                self.assertIsNone((self.f.store.get("pair_runtime:" + pair_id) or {}).get("recovery_watch"))
                if layer == "manager":
                    self.engine.pairs.delete(pair_id)
                else:
                    self.f.store.delete_pair(pair_id)
                self.assertIsNone(self.f.store.pair(pair_id))
                for aid in ("test", "second"):
                    self.assertIsNone(self.f.store.pair_for_account(aid))


class PairOrderRecoveryLiveReadTests(unittest.TestCase):
    setUp = integration.PairSnapshotLifecycleTests.setUp
    tearDown = integration.PairSnapshotLifecycleTests.tearDown
    seed = PairOrderRecoveryTests.seed
    state = PairOrderRecoveryTests.state
    preview = PairOrderRecoveryTests.preview
    confirm = PairOrderRecoveryTests.confirm
    audit = PairOrderRecoveryTests.audit
    no_writes = PairOrderRecoveryTests.no_writes
    broker = PairOrderRecoveryTests.broker

    def test_recovery_budget_priority_is_established_inside_each_get_worker(self):
        self.seed()
        observed = []
        with ExitStack() as stack:
            for broker in self.live.values():
                broker.api.budget = RateBudget()
                api_call = broker.api.call
                def checked(method, path, *args, broker=broker, api_call=api_call, **kwargs):
                    self.assertTrue(getattr(broker.api.budget.priority, "reconciliation", False), path)
                    observed.append(path)
                    return api_call(method, path, *args, **kwargs)
                stack.enter_context(patch.object(broker.api, "call", side_effect=checked))
            self.confirm(self.preview()["token"])
        self.assertIn("/fapi/v3/order", observed)
        self.assertIn("/fapi/v3/allOrders", observed)
        self.assertIn(ORDERS, observed)
        self.assertIn(ACCOUNT, observed)

    def test_live_evidence_reads_original_ids_history_and_all_account_open_orders(self):
        original = self.seed()
        with self.no_writes():
            review = self.preview()
            self.confirm(review["token"])
        for leg in original["pending"]["legs"]:
            calls = self.broker(leg["key"]).api.calls
            queries = [call for call in calls if call[1] == "/fapi/v3/order"]
            self.assertEqual(len(queries), 2)
            self.assertTrue(all(call[2][0] == {"symbol": SYMBOL, "origClientOrderId": leg["order"]["newClientOrderId"]}
                                for call in queries))
            history = [call for call in calls if call[1] == "/fapi/v3/allOrders"]
            self.assertEqual(len(history), 2)
            for _, _, args, kwargs in history:
                self.assertEqual(args[0]["symbol"], SYMBOL)
                self.assertEqual(args[0]["limit"], 1000)
                self.assertEqual(args[0]["startTime"], int((original["pending"]["created_at"] - 60) * 1000))
                self.assertLess(args[0]["endTime"] - args[0]["startTime"], 7 * 86400000)
                self.assertEqual(kwargs, {"signed": True, "weight": 5})
            self.assertGreaterEqual(sum(call[1] == ORDERS for call in calls), 2)
            for call in calls:
                if call[1] == ORDERS:
                    self.assertFalse(call[2])  # Empty parameter list checks every symbol.
        self.assertIsNone(self.state()["pending"])

    def test_live_history_missing_identity_duplicate_id_or_wrong_type_is_rejected(self):
        original = self.seed()
        leg = original["pending"]["legs"][0]
        row = {**receipt_for(leg), "orderId": 123}
        for history in ([row, row], [{**row, "type": "LIMIT"}], [{**row, "time": 1}],
                        [{**row, "origQty": "0.3"}], {"orders": []}, [None]):
            with self.subTest(history=history):
                self.broker().api.responses["/fapi/v3/allOrders"] = history
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), original)

    def test_history_failure_and_full_page_never_allow_archive(self):
        original = self.seed()
        row = receipt_for(original["pending"]["legs"][0], client_id="unrelated")
        for history in (ExchangeError("history network unavailable"), [row] * 1000):
            with self.subTest(history_type=type(history).__name__):
                self.broker().api.responses["/fapi/v3/allOrders"] = history
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertIsNone(self.audit())

    def test_throttled_or_ambiguous_not_found_codes_are_not_negative_evidence(self):
        self.seed()
        for error in (ExchangeError("not found with cooldown", code=-2013, retry_after=30),
                      ExchangeError("gateway not found", code=-2013, http_status=503),
                      AmbiguousOrder("uncertain", code=-2013)):
            with self.subTest(error=str(error)):
                self.broker().api.responses["/fapi/v3/order"] = error
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()

    def test_account_event_between_read_and_commit_revokes_confirmation(self):
        original = self.seed()
        review = self.preview()
        read_members = self.engine.pairs._read_members
        def revoke(*args, **kwargs):
            result = read_members(*args, **kwargs)
            self.live["second"]._cycle_account_event("ORDER_TRADE_UPDATE")
            return result
        with self.no_writes(), patch.object(self.engine.pairs, "_read_members", side_effect=revoke), self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)
        self.assertIsNone(self.audit())

    def test_expired_snapshot_at_database_commit_cannot_archive(self):
        original = self.seed()
        review = self.preview()
        snapshots = []
        read_members = self.engine.pairs._read_members
        commit = self.f.store.commit_pair_recovery
        def capture(*args, **kwargs):
            result = read_members(*args, **kwargs)
            snapshots.extend(result[0].values())
            return result
        def expire(*args, **kwargs):
            snapshots[0].timestamp = time.time() - 60
            return commit(*args, **kwargs)
        with self.no_writes(), patch.object(self.engine.pairs, "_read_members", side_effect=capture), \
                patch.object(self.f.store, "commit_pair_recovery", side_effect=expire), self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)
        self.assertIsNone(self.audit())

    def test_real_account_identity_change_before_final_commit_is_rejected(self):
        original = self.seed()
        review = self.preview()
        commit = self.f.store.commit_pair_recovery
        def replace_identity(*args, **kwargs):
            self.live["test"].api.credentials["user"] = "0x" + "9" * 40
            return commit(*args, **kwargs)
        with self.no_writes(), patch.object(self.f.store, "commit_pair_recovery", side_effect=replace_identity), self.assertRaises(TradingError):
            self.confirm(review["token"])
        self.assertEqual(self.state(), original)
        self.assertIsNone(self.audit())


class PairExplicitOrderRejectionTests(unittest.TestCase):
    tick = trading_tests.PairTradingTests.tick
    snapshots = trading_tests.PairTradingTests.snapshots

    def setUp(self):
        trading_tests.PairTradingTests.setUp(self)
        pair = self.store.pair("gold")
        pair["cycle"]["enabled"] = False
        pair["ordinary"]["enabled"] = True
        self.store.save_pair(pair)
        self.before = {"LONG": "0.3", "SHORT": "0.3"}
        for key, side in (("long", "LONG"), ("short", "SHORT")):
            self.brokers[key].state["positions"][SYMBOL + ":" + side].update(qty="0.3", entry="4412.015")
            self.brokers[key].state["leverages"][SYMBOL] = 20
            self.brokers[key].save()
        self.store.put("pair_runtime:gold", {**runtime_default(), "owned": self.before,
                                             "progress": empty_progress(self.before)})

    def test_two_explicit_new_order_rejections_finish_failed_batch_without_unknown(self):
        with patch.object(self.brokers["long"], "submit", side_effect=ExchangeError("new order rejected", code=-2010)), \
                patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("new order rejected", code=-2010)):
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(state["owned"], self.before)
        history = self.store.get("pair_batch:" + state["last_batch"]["id"])
        for leg in history["legs"]:
            self.assertEqual(leg["receipt"]["status"], "REJECTED")
            self.assertEqual(leg["receipt"]["executedQty"], "0")
            self.assertEqual(leg["receipt"]["reject_code"], -2010)
            self.assertIn("new order rejected", leg["receipt"]["reject_reason"])
            self.assertFalse(leg["receipt"].get("local_not_sent"))
        self.assertTrue(all(not broker.state["orders"] for broker in self.brokers.values()))

    def test_one_new_order_rejection_reduces_only_the_successful_side(self):
        broker = self.brokers["long"]
        with patch.object(broker, "submit", wraps=broker.submit) as long_send, \
                patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("new order rejected", code=-2010)) as short_send:
            state = self.tick()
            self.assertEqual(state["phase"], "repairing", state)
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(state["owned"], self.before)
        self.assertEqual(short_send.call_count, 1)
        self.assertEqual([call.args[0][0]["side"] for call in long_send.call_args_list], ["BUY", "SELL"])
        for key, side, index in (("long", "LONG", 0), ("short", "SHORT", 1)):
            self.assertEqual(self.snapshots()[key].pair(SYMBOL)[index].qty, dec(self.before[side]))

    def test_ambiguous_error_keeps_unknown_even_when_code_matches_rejection(self):
        with ExitStack() as stack:
            for broker in self.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=AmbiguousOrder("unknown delivery", code=-2010)))
                stack.enter_context(patch.object(broker, "query", side_effect=ExchangeError("not found", code=-2013)))
            state = self.tick()
        self.assertEqual(state["phase"], "reconciling", state)
        self.assertIsNotNone(state["pending"])
        self.assertTrue(all(leg["receipt"] is None for leg in state["pending"]["legs"]))
        self.assertEqual(state["owned"], self.before)
