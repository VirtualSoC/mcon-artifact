"""Concurrent cold-boot provisioning  ->  fig/container_boot_time.pdf.

For each density N, cold-boot the root instance and provision N tenants
concurrently, measuring total provision time (first request issued -> last
tenant operational). Cold boot here means each trial starts from a clean
userdata image, so provisioning includes creating each tenant's context and
hotplugging its virtual device (matching the paper's definition for MCon).
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..schema import Record, write_records

EXPERIMENT = "provision_concurrent"


def select_best_interval(
    throughputs: Dict[float, List[float]],
    successes: Dict[float, int],
    trials: int,
) -> Optional[float]:
    """Select the fastest interval that succeeds in every trial."""
    eligible = [
        interval
        for interval, count in successes.items()
        if count == trials and throughputs[interval]
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda interval: (statistics.median(throughputs[interval]), -interval),
    )


def _actual_issue_gap(summary) -> Optional[float]:
    issued = sorted(
        tenant.issued_monotonic_s
        for tenant in summary.tenants
        if tenant.issued_monotonic_s is not None
    )
    gaps = [issued[index + 1] - issued[index] for index in range(len(issued) - 1)]
    return statistics.median(gaps) if gaps else None


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
    intervals: List[float],
    ready_poll_interval: float,
    boot_timeout: float,
    ready_timeout: float,
    reset_between_trials: bool,
    densities: List[int],
) -> tuple[bool, List[Record]]:
    """Sweep issue intervals at density n and retain the best successful setting.

    A density counts as OK only if EVERY trial provisions all n tenants (matches
    the paper's max-density definition) at the selected interval. A short settle
    follows a failed trial so an over-capacity attempt does not poison the next
    boot.
    """
    recs: List[Record] = []
    throughputs: Dict[float, List[float]] = {interval: [] for interval in intervals}
    successes: Dict[float, int] = {interval: 0 for interval in intervals}
    successful_trials = {interval: [] for interval in intervals}

    for interval in intervals:
        for trial in range(trials):
            if reset_between_trials:
                _prime_pool(cfg, driver, densities)

            json_out = out_dir / (
                f"{driver.name}_provision_n{n}_i{interval:g}_t{trial}.json"
            )
            summary = driver.provision(
                n,
                interval=interval,
                ready_poll_interval=ready_poll_interval,
                boot_timeout=boot_timeout,
                ready_timeout=ready_timeout,
                json_out=json_out,
            )
            driver.teardown()

            if not summary:
                print(
                    f"[provision] N={n} interval={interval:g}s trial={trial}: "
                    "provisioning failed"
                )
                time.sleep(5)
                continue

            ready = summary.ready_count
            total = summary.total_s
            print(
                f"[provision] N={n} interval={interval:g}s trial={trial}: "
                f"ready={ready}/{n} total={total}"
            )
            if total is None or ready != n:
                time.sleep(5)
                continue

            successes[interval] += 1
            throughput = n / total
            throughputs[interval].append(throughput)
            successful_trials[interval].append((trial, summary))
            recs.append(
                Record(
                    system=driver.name,
                    experiment=EXPERIMENT,
                    x_name="density",
                    x_value=n,
                    metric="throughput_tenants_s",
                    value=throughput,
                    trial=trial,
                    extra={
                        "issue_interval_s": interval,
                        "requested": n,
                    },
                )
            )
            actual_gap = _actual_issue_gap(summary)
            if actual_gap is not None:
                recs.append(
                    Record(
                        system=driver.name,
                        experiment=EXPERIMENT,
                        x_name="density",
                        x_value=n,
                        metric="actual_issue_gap_s",
                        value=actual_gap,
                        trial=trial,
                        extra={"issue_interval_s": interval},
                    )
                )
            time.sleep(2)

        recs.append(
            Record(
                system=driver.name,
                experiment=EXPERIMENT,
                x_name="density",
                x_value=n,
                metric="success_rate",
                value=successes[interval] / trials,
                extra={
                    "issue_interval_s": interval,
                    "trials": trials,
                },
            )
        )

    selected = select_best_interval(throughputs, successes, trials)
    if selected is None:
        print(f"[provision] N={n}: no interval succeeded in every trial")
        return False, recs

    median_throughput = statistics.median(throughputs[selected])
    median_total = statistics.median(
        float(summary.total_s)
        for _, summary in successful_trials[selected]
        if summary.total_s is not None
    )
    print(
        f"[provision] N={n}: selected interval={selected:g}s "
        f"median_total={median_total:.3f}s"
    )
    recs.append(
        Record(
            system=driver.name,
            experiment=EXPERIMENT,
            x_name="density",
            x_value=n,
            metric="selected_interval_s",
            value=selected,
            extra={
                "criterion": "highest median completion throughput among 100% successful candidates",
                "median_throughput_tenants_s": median_throughput,
                "median_total_latency_s": median_total,
            },
        )
    )

    for trial, summary in successful_trials[selected]:
        recs.append(
            Record(
                system=driver.name,
                experiment=EXPERIMENT,
                x_name="density",
                x_value=n,
                metric="total_latency_s",
                value=float(summary.total_s),
                trial=trial,
                extra={
                    "requested": n,
                    "ready": summary.ready_count,
                    "interval_s": selected,
                    "ready_poll_interval_s": ready_poll_interval,
                },
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
                        trial=trial,
                        extra={
                            "handle": tenant.handle,
                            "interval_s": selected,
                            "issued_at_s": tenant.issued_at_s,
                            "ready_at_s": tenant.ready_at_s,
                            "boot_completed": tenant.boot_completed,
                            "launcher_started": tenant.launcher_started,
                        },
                    )
                )
    return True, recs


def run(cfg: Config, driver, out_dir: Path) -> Path:
    densities: List[int] = sorted(set(cfg.get("sweep.densities", [1, 2, 4, 8])))
    trials: int = int(cfg.get("sweep.trials", 1))
    intervals = sorted(
        set(
            float(value)
            for value in cfg.get(
                "experiments.provision_concurrent.intervals_s",
                [0, 0.1, 0.25, 0.5, 1, 2, 4],
            )
        )
    )
    if not intervals:
        raise SystemExit("experiments.provision_concurrent.intervals_s must not be empty")
    ready_poll_interval = float(
        cfg.get("experiments.provision_concurrent.ready_poll_interval_s", 0.1)
    )
    boot_timeout = float(cfg.get("experiments.provision_concurrent.boot_timeout_s", 180.0))
    ready_timeout = float(
        cfg.get("experiments.provision_concurrent.ready_timeout_s", boot_timeout)
    )
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
            cfg,
            driver,
            out_dir,
            n,
            trials,
            intervals,
            ready_poll_interval,
            boot_timeout,
            ready_timeout,
            reset_between_trials,
            densities,
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
                cfg,
                driver,
                out_dir,
                mid,
                trials,
                intervals,
                ready_poll_interval,
                boot_timeout,
                ready_timeout,
                reset_between_trials,
                densities,
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
