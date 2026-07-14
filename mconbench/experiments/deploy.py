"""App deployment time  ->  fig/container_install_time.pdf.

For each density N, provision N tenants and measure the end-to-end time to
deploy the top-50 apps to all of them. MCon deploys by installing each app
*once* on user 0 and then logically mapping it into each tenant with
`pm install-existing` (near-`O(1)` in N), whereas per-tenant stacks must copy
and install every app N times (`O(N)`).

Each density starts from a clean userdata image (apps must be absent so the
package-name diff on user 0 is correct), then re-warms the namespace pool before
hotplugging the tenants.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

from ..config import Config
from ..schema import Record, write_records

EXPERIMENT = "deploy"


def _collect_apps(apps_dir: Path, max_apps) -> List[Path]:
    files = sorted(
        p for p in apps_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".apk", ".xapk"}
    )
    if not files:
        raise SystemExit(f"no .apk/.xapk files found under {apps_dir}")
    if max_apps:
        files = files[: int(max_apps)]
    return files


def _prime(driver, n: int) -> None:
    """Clean state, then pre-create capacity for N tenants (MCon: warm pool).

    Each density starts from a clean slate so apps are absent (needed for MCon's
    user-0 package-name diff to be correct); ``prepare_pool`` is a no-op for the
    per-tenant baselines.
    """
    driver.reset(capacity=n)
    driver.prepare_pool(n)


def run(cfg: Config, driver, out_dir: Path) -> Path:
    densities: List[int] = cfg.get("sweep.densities", [1, 2, 4])
    trials: int = int(cfg.get("sweep.trials", 1))
    autoscale: bool = bool(cfg.get("sweep.autoscale", True))
    apps_dir = Path(cfg.get("experiments.deploy.apps_dir"))
    max_apps = cfg.get("experiments.deploy.max_apps")
    interval = float(cfg.get("experiments.deploy.provision_interval_s", 0.5))
    boot_timeout = float(cfg.get("experiments.deploy.boot_timeout_s", 180.0))

    if not apps_dir.exists():
        raise SystemExit(f"apps_dir not found: {apps_dir}")
    app_files = _collect_apps(apps_dir, max_apps)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: List[Record] = []
    max_density = 0
    out_csv = out_dir / f"{driver.name}_{EXPERIMENT}.csv"
    print(f"[deploy] {len(app_files)} app package(s) from {apps_dir}")

    for n in densities:
        print(f"[deploy] density={n} ({trials} trial(s))")
        density_ok = True
        for t in range(trials):
            try:
                _prime(driver, n)
            except SystemExit as exc:
                # A prepare_pool/boot failure must not discard the whole run's data
                # (previously a single failed prime at high density crashed the
                # process before write_records). Treat it as a failed density.
                print(f"[deploy] N={n} trial={t}: prime failed ({exc}); stopping density")
                density_ok = False
                try:
                    driver.teardown()
                except Exception:
                    pass
                break

            json_out = out_dir / f"{driver.name}_deploy_n{n}_t{t}.json"
            summary = driver.provision(n, interval=interval, boot_timeout=boot_timeout, json_out=json_out)
            handles = summary.ready_handles() if summary else []
            if len(handles) < n:
                print(f"[deploy] N={n} trial={t}: only {len(handles)}/{n} tenants ready; skipping")
                density_ok = False
                driver.teardown()
                continue

            result = driver.deploy(app_files, handles)
            driver.teardown()

            total = result["total_s"]
            print(
                f"[deploy] N={n} trial={t}: installed {result['n_installed']}/{result['n_attempted']} apps, "
                f"physical={result['physical_s']:.1f}s map={result['map_s']:.1f}s total={total:.1f}s"
            )
            if result.get("errors"):
                print(f"[deploy] N={n} trial={t}: {len(result['errors'])} tenant(s) had install errors")

            records.append(
                Record(
                    system=driver.name,
                    experiment=EXPERIMENT,
                    x_name="density",
                    x_value=n,
                    metric="deploy_total_s",
                    value=float(total),
                    trial=t,
                    extra={
                        "apps": result["n_installed"],
                        "attempted": result["n_attempted"],
                        "tenants": len(handles),
                        "physical_s": round(result["physical_s"], 3),
                        "map_s": round(result["map_s"], 3),
                    },
                )
            )
            time.sleep(2)

        # Persist after every density so a later crash cannot discard completed
        # data (the CSV is rewritten in full each time; cheap at this scale).
        write_records(out_csv, records)
        if density_ok:
            max_density = n
        elif autoscale:
            print(f"[deploy] density {n} failed; stopping sweep (max_density={max_density})")
            break

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

    write_records(out_csv, records)
    print(f"[deploy] wrote {len(records)} records -> {out_csv}")
    return out_csv
