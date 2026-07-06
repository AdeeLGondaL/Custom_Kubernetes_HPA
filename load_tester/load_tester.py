import argparse
import asyncio
from collections import Counter
import time
import httpx


async def _send(client: httpx.AsyncClient, url: str, image_path: str) -> dict:
    t = time.perf_counter()
    with open(image_path, "rb") as f:
        resp = await client.post(url, files={"file": ("image.jpg", f, "image/jpeg")},
                                 timeout=30.0)
    return {"status": resp.status_code, "latency": time.perf_counter() - t}


def _print_summary(results: list):
    successes = [r for r in results if isinstance(r, dict) and r["status"] == 200]
    failures  = [r for r in results if isinstance(r, dict) and r["status"] != 200]
    errors    = [r for r in results if not isinstance(r, dict)]

    print(f"\nTotal: {len(results)}  Success: {len(successes)}  "
          f"Fail: {len(failures)}  Error: {len(errors)}")

    if failures:
        failure_counts = Counter(r["status"] for r in failures)
        print("Failures by HTTP status: "
              + ", ".join(f"{status}={count}"
                          for status, count in sorted(failure_counts.items())))

    if errors:
        error_counts = Counter(type(e).__name__ for e in errors)
        print("Errors by exception type: "
              + ", ".join(f"{name}={count}"
                          for name, count in sorted(error_counts.items())))
        for error in errors[:3]:
            print(f"  sample {type(error).__name__}: {error}")

    if successes:
        lat = sorted(r["latency"] for r in successes)
        p50 = lat[int(0.50 * len(lat))]
        p95 = lat[int(0.95 * len(lat))]
        p99 = lat[int(0.99 * len(lat))]
        print(f"p50: {p50*1000:.1f}ms  p95: {p95*1000:.1f}ms  p99: {p99*1000:.1f}ms")
    else:
        print("No successful responses.")


async def run(url: str, rate: float, duration: float, image_path: str):
    """Fixed-rate mode: send `rate` req/s for `duration` seconds."""
    interval = 1.0 / rate
    tasks    = []
    t_end    = time.monotonic() + duration

    async with httpx.AsyncClient() as client:
        while time.monotonic() < t_end:
            t_next = time.monotonic() + interval
            tasks.append(asyncio.create_task(_send(client, url, image_path)))
            sleep_for = t_next - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    _print_summary(await asyncio.gather(*tasks, return_exceptions=True))


async def run_workload(url: str, image_path: str, rates: list):
    """Workload-file mode: each value in `rates` is req/s for that second."""
    print(f"Workload: {len(rates)} seconds  "
          f"peak={max(rates)} req/s  "
          f"avg={sum(rates)/len(rates):.1f} req/s  "
          f"total≈{sum(rates)} requests")

    tasks = []
    async with httpx.AsyncClient() as client:
        for second, rate in enumerate(rates, start=1):
            t_second_end = time.monotonic() + 1.0
            if rate > 0:
                interval = 1.0 / rate
                while time.monotonic() < t_second_end:
                    t_next = time.monotonic() + interval
                    tasks.append(asyncio.create_task(_send(client, url, image_path)))
                    sleep_for = t_next - time.monotonic()
                    if sleep_for > 0:
                        await asyncio.sleep(sleep_for)
            else:
                await asyncio.sleep(max(0.0, t_second_end - time.monotonic()))

            if second % 30 == 0:
                print(f"  t={second}s  rate={rate} req/s  queued={len(tasks)}")

    _print_summary(await asyncio.gather(*tasks, return_exceptions=True))


def main():
    parser = argparse.ArgumentParser(description="Load tester for the inference dispatcher")
    parser.add_argument("--url",      default="http://localhost:8080/infer")
    parser.add_argument("--rate",     type=float, default=10.0,
                        help="Requests per second (fixed-rate mode)")
    parser.add_argument("--duration", type=float, default=120.0,
                        help="Test duration in seconds (fixed-rate mode)")
    parser.add_argument("--image",    default="test_image.jpg",
                        help="Path to a JPEG image file")
    parser.add_argument("--workload", default=None,
                        help="Path to a workload file — space-separated req/s values, "
                             "one per second. Overrides --rate and --duration.")
    args = parser.parse_args()

    if args.workload:
        with open(args.workload) as f:
            rates = [int(x) for x in f.read().split()]
        asyncio.run(run_workload(args.url, args.image, rates))
    else:
        asyncio.run(run(args.url, args.rate, args.duration, args.image))


if __name__ == "__main__":
    main()
