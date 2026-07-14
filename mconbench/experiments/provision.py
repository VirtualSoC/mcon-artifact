"""Concurrent cold-boot provisioning  ->  fig/container_boot_time.pdf.

For each density N, cold-boot the root instance and provision N tenants
concurrently, measuring total provision time (first request issued -> last
tenant operational). Cold boot here means each trial starts from a clean
userdata image, so provisioning includes creating each tenant's context and
hotplugging its virtual device (matching the paper's definition for MCon).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

from ..config import Config
from ..schema import Record, write_records

EXPERIMENT = "provision_concurrent"


def _prime_pool(cfg: Config, driver, densities: List[int]) -> None:
    """Un-measured pool setup so the measured provision has capacity to attach to.

    For MCon this warms the framework namespace pool (accounts CON-2..); for
    baselines ``prepare_pool`` is a no-op. ``prime_warm=auto`` sizes the pool for
    the largest density in the sweep.
    """
    prime = cfg.get("experiments.provision_concurrent.prime_warm", "auto")
    if prime in (None, 0, "0", False):
        return
    n_tenants = max(densities) if prime == "auto" else int(prime)
    if bool(cfg.get("experiments.provision_concurrent.reset_before_prime", False)):
        print("[provision] resetting to a clean image before priming")
        driver.reset(capacity=max(densities))
    print(f"[provision] priming pool for up to {n_tenants} tenant(s)")
    driver.prepare_pool(n_tenants)


def _run_density(
    cfg: Config,
    driver,
    out_dir: Path,
    n: int,
    trials: int,
    interval: float,
    boot_timeout: float,
    reset_between_trials: bool,
    densities: List[int],
) -> tuple[bool, List[Record]]:
    """Run `trials` trials at density n; return (all_trials_fully_ready, records).

    A density counts as OK only if EVERY trial provisions all n tenants (matches
    the paper's max-density definition). A short settle follows a failed trial so
    an over-capacity attempt (which may crash a worker) does not poison the next
    boot.
    """
    recs: List[Record] = []
    density_ok = True
    for t in range(trials):
        if reset_between_trials:
            _prime_pool(cfg, driver, densities)

        json_out = out_dir / f"{driver.name}_provision_n{n}_t{t}.json"
        summary = driver.provision(n, interval=interval, boot_timeout=boot_timeout, json_out=json_out)
        driver.teardown()

        if not summary:
            print(f"[provision] N={n} trial={t}: provisioning failed")
            density_ok = False
            time.sleep(5)
            continue

        ready = summary.ready_count
        total = summary.total_s
        print(f"[provision] N={n} trial={t}: ready={ready}/{n} total={total}")

        if total is not None and ready == n:
            recs.append(
                Record(
                    system=driver.name,
                    experiment=EXPERIMENT,
                    x_name="density",
                    x_value=n,
                    metric="total_latency_s",
                    value=float(total),
                    trial=t,
                    extra={"requested": n, "ready": ready, "interval_s": interval},
                )
            )
        for tenant in summary.tenants:
            if tenant.duration_s is not None and tenant.ready:
                recs.append(
                    Record(
                        system=driver.name,
                        experiment=EXPERIMENT,
                        x_name="density",
                        x_value=n,
                        metric="tenant_latency_s",
                        value=float(tenant.duration_s),
                        trial=t,
                        extra={"handle": tenant.handle},
                    )
                )
        if ready < n:
            density_ok = False
            time.sleep(5)  # extra settle: an over-capacity attempt may have crashed a worker
        else:
            time.sleep(2)
    return density_ok, recs


def run(cfg: Config, driver, out_dir: Path) -> Path:
    densities: List[int] = sorted(set(cfg.get("sweep.densities", [1, 2, 4, 8])))
    trials: int = int(cfg.get("sweep.trials", 1))
    interval: float = float(cfg.get("experiments.provision_concurrent.interval_s", 1.0))
    boot_timeout = float(cfg.get("experiments.provision_concurrent.boot_timeout_s", 180.0))
    autoscale: bool = bool(cfg.get("sweep.autoscale", True))
    reset_between_trials: bool = bool(cfg.get("sweep.reset_between_trials", False))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: List[Record] = []
    max_density = 0
    first_fail: int | None = None

    if not reset_between_trials:
        _prime_pool(cfg, driver, densities)

    for n in densities:
        print(f"[provision] density={n} ({trials} trial(s))")
        ok, recs = _run_density(
            cfg, driver, out_dir, n, trials, interval, boot_timeout, reset_between_trials, densities
        )
        records.extend(recs)
        if ok:
            max_density = n
        else:
            first_fail = n
            if autoscale:
                print(f"[provision] density {n} failed (max so far={max_density})")
                break

    # Bisect the true ceiling between the last fully-good density and the first
    # failed one. The powers-of-two sweep alone under-reports (e.g. it would say
    # 32 when the hardware actually sustains ~47); bisection recovers the exact max.
    if autoscale and first_fail is not None and max_density >= 1 and first_fail - max_density > 1:
        lo, hi = max_density, first_fail
        print(f"[provision] bisecting max density in ({lo}, {hi})")
        while hi - lo > 1:
            mid = (lo + hi) // 2
            print(f"[provision] bisect: density={mid} (good={lo}, bad={hi})")
            ok, recs = _run_density(
                cfg, driver, out_dir, mid, trials, interval, boot_timeout, reset_between_trials, densities
            )
            records.extend(recs)
            if ok:
                lo = mid
            else:
                hi = mid
        max_density = lo

    print(f"[provision] max_density={max_density}")
    records.append(
        Record(
            system=driver.name,
            experiment=EXPERIMENT,
            x_name="density",
            x_value=max_density,
            metric="max_density",
            value=max_density,
        )
    )

    out_csv = out_dir / f"{driver.name}_{EXPERIMENT}.csv"
    write_records(out_csv, records)
    print(f"[provision] wrote {len(records)} records -> {out_csv}")
    return out_csv
