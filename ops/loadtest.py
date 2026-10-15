"""Bounded load test for the beta read paths.

Measures latency and error rate against a running instance and fails when either crosses
the documented threshold, so "expected load" is a number that can be re-checked rather than
an assumption. Absolute latency on a laptop says little; the value is the same shape of
traffic being repeatable before and after a change, and on the machine that will serve beta.

Thresholds are deliberately loose defaults for a single development machine:

    p95 latency  <= 250 ms per request
    error rate   <= 1% of requests

Usage:

    python ops/loadtest.py                                  # local server, 20s, 10 workers
    python ops/loadtest.py --seconds 60 --concurrency 25
    python ops/loadtest.py --base-url http://127.0.0.1:8001 --paths /healthz /login

Only read paths are exercised by default. Point it at a scratch instance before adding
authenticated paths, because a mutating path would create real data.
"""

import argparse
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

DEFAULT_PATHS = ("/healthz", "/readyz", "/login")
P95_BUDGET_MS = 250.0
ERROR_RATE_BUDGET = 0.01
REQUEST_TIMEOUT_SECONDS = 10.0


def run_worker(
    base_url: str,
    paths: tuple[str, ...],
    stop_at: float,
    latencies: list[float],
    statuses: list[int],
    failures: list[str],
    lock: threading.Lock,
) -> None:
    local_latencies: list[float] = []
    local_statuses: list[int] = []
    local_failures: list[str] = []
    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        index = 0
        while time.perf_counter() < stop_at:
            path = paths[index % len(paths)]
            index += 1
            started = time.perf_counter()
            try:
                response = client.get(path)
            except httpx.HTTPError as error:
                local_failures.append(f"{path}: {type(error).__name__}")
                continue
            local_latencies.append((time.perf_counter() - started) * 1000)
            local_statuses.append(response.status_code)
    with lock:
        latencies.extend(local_latencies)
        statuses.extend(local_statuses)
        failures.extend(local_failures)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--paths", nargs="+", default=list(DEFAULT_PATHS))
    parser.add_argument("--p95-budget-ms", type=float, default=P95_BUDGET_MS)
    parser.add_argument("--error-rate-budget", type=float, default=ERROR_RATE_BUDGET)
    arguments = parser.parse_args()

    if arguments.concurrency < 1 or arguments.seconds <= 0:
        parser.error("--concurrency must be at least 1 and --seconds must be positive")

    paths = tuple(arguments.paths)
    latencies: list[float] = []
    statuses: list[int] = []
    failures: list[str] = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + arguments.seconds

    print(
        f"load: {arguments.concurrency} workers for {arguments.seconds:g}s "
        f"against {arguments.base_url} on {', '.join(paths)}"
    )
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=arguments.concurrency) as pool:
        for _ in range(arguments.concurrency):
            pool.submit(
                run_worker,
                arguments.base_url,
                paths,
                stop_at,
                latencies,
                statuses,
                failures,
                lock,
            )
    elapsed = time.perf_counter() - started

    total = len(statuses) + len(failures)
    errors = sum(1 for status in statuses if status >= 500) + len(failures)
    error_rate = errors / total if total else 1.0
    p95 = percentile(latencies, 0.95)

    print(f"requests: {total} in {elapsed:.1f}s ({total / elapsed:.0f}/s)")
    print(
        f"latency ms: p50={statistics.median(latencies) if latencies else 0:.1f} "
        f"p95={p95:.1f} p99={percentile(latencies, 0.99):.1f} "
        f"max={max(latencies) if latencies else 0:.1f}"
    )
    status_summary = ", ".join(f"{code}={statuses.count(code)}" for code in sorted(set(statuses)))
    print(f"statuses: {status_summary or 'none'}")
    if failures:
        print(f"transport failures: {len(failures)} (first: {failures[0]})")

    breaches = []
    if p95 > arguments.p95_budget_ms:
        breaches.append(f"p95 {p95:.1f}ms exceeds the {arguments.p95_budget_ms:g}ms budget")
    if error_rate > arguments.error_rate_budget:
        breaches.append(
            f"error rate {error_rate:.2%} exceeds the {arguments.error_rate_budget:.2%} budget"
        )
    if not total:
        breaches.append("no requests completed")

    if breaches:
        for breach in breaches:
            print(f"FAIL: {breach}")
        return 1

    print("PASS: within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
