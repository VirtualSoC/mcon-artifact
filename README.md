# MCon — SOSP'26 Artifact

This is the artifact README for MCon---an Android container system that shares a single framework across tenants.

This artifact reproduces MCon's headline **elasticity** results: fast tenant
provisioning, near-`O(1)` app deployment, and high app throughput / instance
density versus per-tenant container and emulator stacks.

Each experiment writes a canonical CSV and plots one paper figure:

| Claim (paper §) | Experiment (`run`) | Figure (`plot`) | Expected qualitative trend |
|---|---|---|---|
| Sub-second, scalable provisioning (§6.2) | `provision_concurrent` | `container_boot_time.pdf` | MCon ~1 s @ N=1, near-linear; baselines >15 s floor |
| Near-`O(1)` deployment (§6.3) | `deploy` | `container_install_time.pdf` | MCon grows ~1.7x from 1→64; baselines `O(N)` |
| High throughput & density (§6.4) | `fps` | `fps.pdf` | MCon sustains FPS to higher N than baselines |

## Hardware note

The paper uses a dual-socket Intel Xeon 4210 Silver CPUs (2.20GHz),
    a NVIDIA RTX A4000 GPU with 16 GiB VRAM,
    and 128 GiB of DDR4 ECC memory.
Absolute numbers (max density, peak FPS, crash points) will vary depending on the CPU / memory / GPU combination — but the qualitative claims should hold.

## Step 1 — Set up the environment

**Prerequisites (host):** An Ubuntu 20.04 or greater Linux machine with >= 16 GiB memory and a NVIDIA GPU is required. Free disk space of at least 2 TiB is required if you want to reproduce the performance of all the baselines.

**Clone and install this repo:**

```bash
git clone https://github.com/VirtualSoC/mcon-artifact.git
cd mcon-artifact
chmod +x cmd
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

**Build the stacks.** From the cloned checkout, follow **[docs/setup.md](docs/setup.md)**
to build MCon/vSoC and, if needed, prepare the baseline stacks.

**Point the code at your built tree:**

```bash
cp env.example .env && $EDITOR .env      # set BASE_DIR (required) and GUEST_IMG_PATH
source .env                              # REQUIRED: our code reads these from the environment
```

`.env` is **not** auto-loaded — `source` it (or otherwise export `BASE_DIR`
etc.) in every shell before running `mconbench`. The `config/*.yaml` files expand
`${BASE_DIR}` and friends from the environment.

## Step 2 — Download the app corpus

The `deploy` and `fps` experiments needs the paper's top-50 app corpus
(~6.2 GB of APK/XAPK files) installed into `$BASE_DIR/mcon-artifact/apps/` (the
`experiments.*.apps_dir` in every config). Download and unpack it with:

```bash
# ~6.2 GB, split across parts on a GitHub release; fetch, verify, and extract:
bash scripts/fetch_apps.sh
```

The script downloads each part, verifies SHA-256 checksums, reassembles the
archive, and extracts it into `apps/`. For a quick check, cap the corpus with
`experiments.*.max_apps` in your config. `provision_concurrent` needs no apps.

[docs/fifty-apps.md](docs/fifty-apps.md) lists detailed information of the fifty apps used.

## Step 3 — Run an experiment

```bash
python -m mconbench run <experiment> --system <system> --config <config>
```

- `<experiment>`: `provision_concurrent` | `deploy` | `fps`
- `<system>`: `mcon` | `vsoc` | `redroid` | `anbox` | `gae`
- `<config>`: a preset in [config/](config/) (see below)

Each run writes a canonical CSV to
`data/runs/<timestamp>/<system>_<experiment>.csv` (override the directory with
`--out`).

**Smoke test first (minutes, tiny densities).** Confirms the full stack works end-to-end before committing to a long sweep:

```bash
python -m mconbench run provision_concurrent --system mcon --config config/smoke.yaml
```

**Full paper sweep (hours).** Use `config/default.yaml` (or the per-experiment
`*_sweep.yaml` presets):

```bash
python -m mconbench run provision_concurrent --system mcon --config config/default.yaml
python -m mconbench run deploy               --system mcon --config config/default.yaml
python -m mconbench run fps                  --system mcon --config config/default.yaml
```

Re-run each with `--system vsoc` (and, if configured, `redroid` / `anbox` /
`gae`) to get the baseline curves. On smaller GPUs, trim `sweep.densities` in
the config so over-capacity densities bail out instead of hanging.

### Config presets

| Config | Purpose |
|---|---|
| `smoke.yaml` | tiny densities, 1 trial — live shakeout of any experiment |
| `verify.yaml` | small, fail-fast provisioning check |
| `default.yaml` | full paper sweep (all experiments) |
| `deploy_sweep.yaml` / `fps_sweep.yaml` | full per-experiment sweeps |
| `deploy_validate.yaml` / `fps_validate.yaml` | reduced validation runs |

## Step 4 — Check the results

Turn one or more run directories into figures:

```bash
# one figure from one run
python -m mconbench plot provision_concurrent --data data/runs/<timestamp>/

# every figure it can build from a directory of runs (searched recursively)
python -m mconbench plot all --data data/runs/
```

`--data` takes a CSV file or a directory (all `*.csv` under it are pooled, so you
can point `plot all` at a folder holding several systems' runs to get combined
curves). Figures are written to `data/figures/` by default (`--out` to change).
A plotter with no matching rows prints a note and is skipped rather than writing
an empty figure.

Compare the trends against the paper's figures. Absolute values scale with your hardware (see [Hardware note](#hardware-note)).

## Systems

Baseline connection parameters live under `systems.<name>` in
[config/default.yaml](config/default.yaml); setup is in
[docs/setup.md](docs/setup.md).

| `--system` | Stack |
|---|---|
| `mcon` | MCon (multi-tenant, shared framework) |
| `vsoc` | vSoC — one QEMU instance per tenant |
| `redroid` | Redroid — Docker Android containers in an outer VM (SSH) |
| `anbox` | Anbox Cloud — `amc` containers in a Multipass VM |
| `gae` | Google Android Emulator — stock AVDs |

## Repository layout

```
config/       default.yaml (paths, densities, ports) + smoke/verify/sweep presets
docs/         setup.md (build MCon + baselines)
scripts/      fetch_apps.sh (download corpus) + pack_apps.sh (author-side packing)
mconbench/    schema.py (canonical rows) + CLI, systems/, experiments/, plots/
data/
  runs/       your fresh measurement runs (git-ignored)
  figures/    figures written by `plot` (git-ignored)
env.example   copy to .env; sets BASE_DIR, ports, baseline endpoints
```

## Troubleshooting

- **`mconbench` can't find the tree / `${BASE_DIR}` unexpanded:** you didn't
  `source .env` in this shell (Step 1).
- **No devices / adb connection:** check `adb devices`; if empty, run `adb
  kill-server` and retry.
- **A density hangs or the GPU runs out of memory:** lower `sweep.densities` in
  your config; the fail-fast presets (`verify.yaml`, `*_sweep.yaml`) bail out of
  over-capacity densities faster than `default.yaml`.
