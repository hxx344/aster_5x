"""Local, deterministic risk/health benchmarks; never connects to an exchange."""
import argparse
import asyncio
import json
import platform
from statistics import median
from time import perf_counter_ns

import httpx

from trading.engine import Engine
from trading.models import dec, plan_pair
from trading.server import create_app
from .helpers import Fixture


def distribution(samples):
    ordered = sorted(samples)
    return {"iterations": len(samples), "median_ms": round(median(samples), 4),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 4),
            "max_ms": round(max(samples), 4)}


def measure_plan(fixture, iterations):
    snapshot = fixture.broker.snapshot(["XAUUSD1"])
    book = fixture.market.book("XAUUSD1")
    rule = fixture.market.rules["XAUUSD1"]
    samples = []
    for index in range(iterations + 20):
        started = perf_counter_ns()
        plan = plan_pair(snapshot, book, rule, {4: dec(500000)}, fixture.account["policy"], now=book.timestamp)
        elapsed = (perf_counter_ns() - started) / 1_000_000
        if not plan.qty:
            raise RuntimeError("Benchmark scenario unexpectedly blocked")
        if index >= 20:
            samples.append(elapsed)
    return distribution(samples)


async def measure_health(fixture, iterations):
    engine = Engine(fixture.store, market=fixture.market)
    engine.ready = True
    app = create_app(engine, start_engine=False)
    batches = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid") as client:
        for _ in range(3):
            started = perf_counter_ns()
            for _ in range(iterations):
                response = await client.get("/api/health")
                if response.status_code != 200:
                    raise RuntimeError("Benchmark health check failed")
            batches.append((perf_counter_ns() - started) / 1_000_000)
    return {"requests_per_batch": iterations, "batches_ms": [round(value, 2) for value in batches],
            "median_batch_ms": round(median(batches), 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2000)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10000:
        parser.error("iterations must be between 1 and 10000")
    fixture = Fixture()
    try:
        for key, row in fixture.broker.state["positions"].items():
            symbol = key.split(":")[0]
            row.update(qty="1", entry=str(fixture.market.book(symbol).mark))
        normal = measure_plan(fixture, args.iterations)
        fixture.market.rules["XAUUSD1"].step = dec("1e-100")
        extreme = measure_plan(fixture, min(100, args.iterations))
        health = asyncio.run(measure_health(fixture, min(1000, args.iterations)))
        print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                          "normal_plan": normal, "extreme_step_plan": extreme, "asgi_health": health}, indent=2))
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
