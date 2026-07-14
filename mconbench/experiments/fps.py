"""Application throughput (FPS vs. tenant density)  ->  fig/fps.pdf.

For each density N, provision N tenants, deploy the top-50 app corpus to all of
them, then run a number of measurement rounds. Each round starts one app per
tenant, drives it with `monkey`, and reads `dumpsys gfxinfo` to derive FPS from
the frame-time histogram (identical methodology to the paper's evaluate.py,
which mcon_measure_sweep.py drives). We report the mean FPS across the
successfully-rendering tenant instances, one row per round.

Two workload patterns (paper section 6.4):
  random      : each tenant runs a distinct app (sampled without replacement).
                This is the default: it reflects the realistic multi-tenant case
                and avoids a same-package process collision on the shared build
                (see _assign).
  round-robin : every tenant runs the same app in a round; apps cycle per round.
                Kept for parity with the paper, but on this build all tenants of
                one package collapse onto a single app process, so only one tenant
                renders -- use `random` for meaningful per-density numbers here.

The FPS claim is that MCon sustains high per-app FPS as N grows (framework
consolidation avoids the memory cliff baselines hit), degrading gracefully only
once the GPU saturates -- so the metric of interest is avg_fps as a function of
density, plus the max density that still renders.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from statistics import mean
from typing import Dict, List

from ..config import Config
from ..schema import Record, write_records

EXPERIMENT = "fps"


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
    """Clean state, then pre-create capacity for N tenants (MCon: warm pool)."""
    driver.reset(capacity=n)
    driver.prepare_pool(n)


def _assign(pattern: str, handles: List, packages: List[str], r: int, rng) -> Dict:
    """Pick which app each tenant runs this round.

    In the random pattern we assign *distinct* apps to tenants (sampling without
    replacement) whenever the corpus is large enough. This is deliberate: on the
    shared-framework build, launching the *same* package for multiple Android
    users collapses onto a single app process (only one tenant ends up with a
    live process), so a naive independent draw that repeats an app would silently
    drop tenants from the measurement. Distinct assignment keeps one live process
    per tenant, which is also the realistic multi-tenant case (different users run
    different apps). round-robin (all tenants run one app) is kept for parity with
    the paper but exhibits the single-process collision on this build. (Baselines
    are one-instance-per-tenant, so neither pattern collides there.)
    """
    n = len(handles)
    if pattern == "round-robin":
        app = packages[r % len(packages)]
        return {h: app for h in handles}
    # random: distinct app per tenant when possible, else fall back to independent
    # draws (unavoidable repeats once density exceeds the corpus size).
    if len(packages) >= n:
        picks = rng.sample(packages, n)
    else:
        picks = [rng.choice(packages) for _ in range(n)]
    return {h: picks[i] for i, h in enumerate(handles)}


def run(cfg: Config, driver, out_dir: Path) -> Path:
    densities: List[int] = cfg.get("sweep.densities", [1, 2, 4])
    trials: int = int(cfg.get("sweep.trials", 1))
    autoscale: bool = bool(cfg.get("sweep.autoscale", True))

    pattern: str = cfg.get("experiments.fps.pattern", "random")
    window_s = float(cfg.get("experiments.fps.measure_window_s", 60.0))
    startup_s = float(cfg.get("experiments.fps.startup_s", 8.0))
    rounds = int(cfg.get("experiments.fps.rounds", 3))
    drive = bool(cfg.get("experiments.fps.drive_input", True))
    seed = int(cfg.get("experiments.fps.seed", 0))
    min_frames = int(cfg.get("experiments.fps.min_frames", 30))
    max_apps = cfg.get("experiments.fps.max_apps")
    apps_dir = Path(cfg.get("experiments.fps.apps_dir") or cfg.get("experiments.deploy.apps_dir"))
    interval = float(cfg.get("experiments.fps.provision_interval_s", 0.5))
    boot_timeout = float(cfg.get("experiments.fps.boot_timeout_s", 180.0))

    if not apps_dir.exists():
        raise SystemExit(f"apps_dir not found: {apps_dir}")
    app_files = _collect_apps(apps_dir, max_apps)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: List[Record] = []
    max_density = 0
    out_csv = out_dir / f"{driver.name}_{EXPERIMENT}.csv"
    rng = random.Random(seed)
    print(f"[fps] pattern={pattern} window={window_s:.0f}s rounds={rounds} corpus={len(app_files)} apps")

    for n in densities:
        print(f"[fps] density={n} ({trials} trial(s))")
        density_ok = True
        for t in range(trials):
            try:
                _prime(driver, n)
            except SystemExit as exc:
                # A prepare_pool/boot failure must not discard the whole run's data
                # (a single failed prime at high density would otherwise crash the
                # process before write_records). Treat it as a failed density.
                print(f"[fps] N={n} trial={t}: prime failed ({exc}); stopping density")
                density_ok = False
                try:
                    driver.teardown()
                except Exception:
                    pass
                break

            json_out = out_dir / f"{driver.name}_fps_n{n}_t{t}.json"
            summary = driver.provision(n, interval=interval, boot_timeout=boot_timeout, json_out=json_out)
            handles = summary.ready_handles() if summary else []
            if len(handles) < n:
                print(f"[fps] N={n} trial={t}: only {len(handles)}/{n} tenants ready; skipping")
                density_ok = False
                driver.teardown()
                continue

            dep = driver.deploy(app_files, handles)
            packages = dep.get("packages") or []
            if not packages:
                print(f"[fps] N={n} trial={t}: no apps installed; skipping")
                density_ok = False
                driver.teardown()
                continue

            for r in range(rounds):
                assignments = _assign(pattern, handles, packages, r, rng)
                per = driver.measure_fps_round(assignments, startup_s, window_s, drive, min_frames=min_frames)
                ok = [m["fps"] for m in per.values() if m["ok"]]
                round_avg = mean(ok) if ok else 0.0
                print(
                    f"[fps] N={n} trial={t} round={r}: avg={round_avg:.1f} fps "
                    f"({len(ok)}/{len(handles)} rendering)"
                )
                records.append(
                    Record(
                        system=driver.name,
                        experiment=EXPERIMENT,
                        x_name="density",
                        x_value=n,
                        metric="avg_fps",
                        value=float(round_avg),
                        trial=t * rounds + r,
                        extra={
                            "pattern": pattern,
                            "tenants": len(handles),
                            "rendering": len(ok),
                            "apps": len(packages),
                            "window_s": window_s,
                            "per_tenant_fps": {
                                str(h): round(m["fps"], 2) for h, m in per.items()
                            },
                        },
                    )
                )

            driver.teardown()
            time.sleep(2)

        # Persist after every density so a later crash cannot discard completed
        # data (the CSV is rewritten in full each time; cheap at this scale).
        write_records(out_csv, records)
        if density_ok:
            max_density = n
        elif autoscale:
            print(f"[fps] density {n} failed; stopping sweep (max_density={max_density})")
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
    print(f"[fps] wrote {len(records)} records -> {out_csv}")
    return out_csv
