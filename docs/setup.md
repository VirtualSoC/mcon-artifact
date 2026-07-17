# Environment setup

Set up MCon and the four baseline stacks **once**, before running any experiment
(Tier 1 and up). This page assumes you have already cloned the artifact repo and
are reading it from that checkout; repo-relative links such as
[`env.example`](../env.example) and [`config/default.yaml`](../config/default.yaml)
refer to files in the clone.

Paths below are relative to the artifact's `BASE_DIR` (see
[`env.example`](../env.example)).

## Host prerequisites

- Linux host with an NVIDIA GPU and driver (version 550 and above), for GPU-accelerated rendering.
- KVM enabled: `/dev/kvm` accessible.
- `adb` on `PATH`, `python3`.

## MCon and vSoC (required)

MCon and the vSoC baseline are built from the **same** source tree and share
a single build. Follow the Linux section of the vSoC build guide:

> **https://github.com/VirtualSoC/vsoc/wiki/Build-vSoC**

When the build finishes you should have `bin/qemu-system-x86_64` and a guest image under `img/`. Then:

1. Point our code at the tree: set `BASE_DIR` (and `GUEST_IMG_PATH`) in your
   `.env` — see [`env.example`](../env.example).
2. The same binary drives both systems — no separate build for the vSoC
   baseline:
   - `--system mcon` — multi-tenant MCon.
   - `--system vsoc` — one QEMU instance per tenant.

## Baselines (optional)

The remaining three baselines each run inside their own outer VM and are only
needed for the full comparison. Enable one in
[`config/default.yaml`](../config/default.yaml) under `systems.<name>` and set
its connection parameters there or in `.env`.

### Redroid

Redroid runs Docker "Android-in-a-container" instances inside an **outer QEMU
Linux VM**, driven over SSH. our code manages only the *inner* containers
(the measured provisioning step); the outer VM is un-measured infrastructure
that must be reachable first. We ship a **prebuilt outer-VM image** so you don't
have to assemble Docker + redroid + the binder kernel modules yourself.

#### 1. Host prerequisites

- KVM + an NVIDIA GPU (as for MCon); the outer VM renders containers via
  `virtio-vga-gl`.
- OVMF firmware plus tools used by the launcher to create the NoCloud seed disk:
  `sudo apt install -y ovmf dosfstools mtools`.
- `sshpass` on the host, only if you use password auth. For the prebuilt image's
  default password flow: `sudo apt install -y sshpass`.

#### 2. Get the outer-VM image

Download the prebuilt **minimal** image (Ubuntu cloud image + staged Docker /
redroid first-boot payload + `init.sh` control script), hosted on the same GitHub
release as the app corpus:

```bash
bash scripts/fetch_redroid_image.sh    # -> $BASE_DIR/img/redroid/redroid.qcow2
```

`redroid.sh` reads `REDROID_IMG_PATH` (default
`$BASE_DIR/img/redroid/redroid.qcow2`), so no extra config is needed if you keep
the default path.


**What's in the prebuilt image (`redroid.qcow2`):** a minimal Ubuntu 24.04 cloud
image plus a first-boot payload. When booted through
[`platform/redroid.sh`](../platform/redroid.sh), the launcher attaches a small
NoCloud seed disk for DHCP/SSH, boots with OVMF, and the guest installs Docker +
GL dependencies, loads the baked `redroid/redroid:13.0.0-ndk` image (Android 13
with libndk arm64 translation), enables `binder_linux`, and exposes
`/home/redroid/init.sh` with `run <count>` / `stop` / `rm`. Container *i*'s adb
is forwarded to host port `5555+i`. Default login for the shipped image: user
`redroid`, password `redroid`.

Prefer to build the image yourself? See
[Build the outer-VM image from scratch](#build-the-outer-vm-image-from-scratch)
below.

#### 3. Start the outer VM

[`platform/redroid.sh`](../platform/redroid.sh) boots the image and forwards SSH
(host `2222` → guest `22`) plus the adb bridge ports `5555-5655`. Size it to your
host — the paper used 36 vCPU / 180 GiB (`BASE_DIR` must be set; `source .env`
first):

```bash
REDROID_VM_CPUS=8 REDROID_VM_MEM=16G bash platform/redroid.sh &
```

The first boot needs outbound network access inside the guest so it can install
Docker/GL packages and load the baked Redroid container image. This can take a
few minutes. The launcher writes boot logs to `$BASE_DIR/log/redroid-serial.log`
and `$BASE_DIR/log/redroid-qemu.log`, and creates
`$BASE_DIR/img/redroid/redroid-seed.img` automatically. If your OVMF firmware is
not in a standard location, set `REDROID_OVMF_PATH=/path/to/OVMF.fd`. For a
headless host with EGL support, set `REDROID_DISPLAY=egl-headless`; otherwise
the default is `sdl,gl=on`.

Or let our code start it for you, in [`config/default.yaml`](../config/default.yaml):

```yaml
systems:
  redroid:
    manage_vm: true
    vm_launch_cmd: "bash platform/redroid.sh"
```

> `redroid.sh` uses `-display sdl,gl=on` by default, which needs an X display
> (e.g. the local GDM/Xorg session). On a truly headless host, run it under a
> virtual display or set `REDROID_DISPLAY=egl-headless`.

#### 4. Point our code at the VM

Set the SSH endpoint (env overrides `config/default.yaml`; keep the password out
of git — shell only):

```bash
export REDROID_SSH_HOST=localhost
export REDROID_SSH_PORT=2222
export REDROID_SSH_USER=redroid
export REDROID_SSH_PASS=redroid        # default for the shipped image
```

Confirm connectivity before a run:

```bash
sshpass -e ssh -o StrictHostKeyChecking=no \
  -p "$REDROID_SSH_PORT" "$REDROID_SSH_USER@$REDROID_SSH_HOST" \
  'docker --version && cd /home/redroid && ./init.sh rm && ./init.sh run 1 && docker ps'
```

If SSH works but `docker` is missing, the image was probably booted once without
the NoCloud seed/network path and the old first-boot service disabled itself
after a failed package install. Recover it once with:

```bash
sshpass -e ssh -o StrictHostKeyChecking=no \
  -p "$REDROID_SSH_PORT" "$REDROID_SSH_USER@$REDROID_SSH_HOST" \
  "printf '%s\n' \"$REDROID_SSH_PASS\" | sudo -S /opt/firstboot.sh"
```

Containers appear on the host as `localhost:5555`, `localhost:5556`, …
(stride 1: container *i* → `base_adb_port + i`, with *i* starting at 0).

#### 5. Verify

```bash
python -m mconbench run provision_concurrent --system redroid --config config/smoke.yaml
python -m mconbench run deploy               --system redroid --config config/smoke.yaml
python -m mconbench run fps                  --system redroid --config config/smoke.yaml
```

#### Build the outer-VM image from scratch

The prebuilt image is fully reproducible with
[`scripts/build_redroid_image.sh`](../scripts/build_redroid_image.sh). It:

1. **builds the redroid image with libndk arm64 translation** — layers the
   `zhouziyang/libndk_translation` v0.2.3 prebuilts (which support Android 13)
   onto `redroid/redroid:13.0.0-latest` and tags it `redroid/redroid:13.0.0-ndk`;
2. **bakes a minimal outer VM** — downloads an Ubuntu 24.04 cloud image and,
   with `virt-customize`, creates the `redroid` SSH user, installs
   [`platform/redroid-guest/init.sh`](../platform/redroid-guest/init.sh), stages
   a fail-fast first-boot service that installs Docker + GL packages, enables
   `binder_linux`, and loads the baked redroid image;
3. **sparsifies/compresses** the result to `redroid.qcow2`.

```bash
# host needs: docker, guestfs-tools (virt-customize), qemu-img, curl, git
source .env
sudo -E bash scripts/build_redroid_image.sh      # -> $BASE_DIR/img/redroid/redroid.qcow2
```

The guest control script
[`platform/redroid-guest/init.sh`](../platform/redroid-guest/init.sh) implements
the `run <count>` / `stop` / `rm` contract the driver calls and launches each
container with the libndk `androidboot` props (container *i* → adb port
`5555+i`).

> On first boot (needs network) the image installs Docker + deps, loads the
> baked redroid image, enables binder, and grows the root filesystem to the 2 TB
> virtual size (`cloud-init growpart`) so a capable host can pack in many
> containers. The qcow2 is thin + compressed, so it downloads small and only
> grows on disk as containers write. If the first `init.sh run` reports binder
> errors, reboot once (the generic kernel + modules install on first boot). For
> FPS parity the guest renders on the outer VM's GPU
> (`androidboot.redroid_gpu_mode=host`).

### Anbox Cloud

Anbox Cloud runs `amc` Android containers under the Anbox Cloud **Appliance**
(the single-machine variant). Two backends are selectable via
`systems.anbox.backend`:

- **`local`** (default) — a **bare-metal** appliance on this host. This is the path MCon uses: containers render on the host's real GPU. See
  *Bare-metal appliance* below.
- **`multipass`** — the appliance inside a Multipass VM. Convenient, but there
  is **no GPU passthrough** (software rendering), so it is fine for
  provisioning/deploy metrics but **not** FPS. See *Provision the appliance VM*.

Either way, our code launches containers via
[`platform/anbox_test.sh`](../platform/anbox_test.sh) and reaches them through
`anbox-connect`, which assigns **dynamic** adb ports — so tenants are discovered
from `adb devices` (`127.0.0.1:*`), not a fixed port map.

> **Licensing:** the Anbox Cloud Appliance needs an **Ubuntu Pro** token (free
> for personal use). Get one at <https://ubuntu.com/pro>.

#### 1. Host prerequisites

```bash
sudo apt install -y tmux
# anbox-connect is delivered as a snap (installs to /snap/bin/anbox-connect);
# install the Anbox Cloud client tooling per the tutorial below, then confirm:
which anbox-connect
```

our code kills `anbox-connect` processes and `anbox_*` tmux sessions on
teardown, so both must be reachable from the shell that runs `mconbench`.

#### 2.1 Bare-metal appliance (`backend: local`, default)

For GPU-accelerated runs, install the appliance directly on a host with an
NVIDIA GPU:

```bash
sudo snap install anbox-cloud-appliance
sudo pro attach <UBUNTU_PRO_TOKEN>
sudo pro enable anbox-cloud --assume-yes
sudo snap refresh lxd --channel=5.21/stable
sudo anbox-cloud-appliance prepare-node-script | sudo bash -e   # binder_linux
sudo anbox-cloud-appliance init --auto
anbox-cloud-appliance status                                    # -> status: ready
cd /tmp && sudo amc image add android15 jammy:android15:amd64 --type container --timeout 20m
```

Confirm the GPU is wired into the node (non-zero `gpu-slots`):

```bash
sudo amc node show lxd0 | grep -E 'gpu-slots|type: nvidia'
# gpu-slots: 32   ...   type: nvidia
```

For our benchmark code to run the appliance without sudo privileges, grant the user a scoped NOPASSWD rule; If you are running as root or are willing to enter a password yourself during the benchmark, you can skip the sudoers rule.

```bash
# /etc/sudoers.d/anbox-scalebench
echo "$USER ALL=(root) NOPASSWD: /snap/bin/amc, /snap/bin/anbox-cloud-appliance.gateway" \
  | sudo tee /etc/sudoers.d/anbox-scalebench >/dev/null
sudo chmod 440 /etc/sudoers.d/anbox-scalebench
sudo visudo -cf /etc/sudoers.d/anbox-scalebench    # validate syntax
sudo -n amc ls                                     # sanity: no password prompt
```

#### 2.2 Provision the appliance VM (Skip if using `backend: local`)

First, install the Multipass hypervisor (Ubuntu 22.04+ ships it as a snap):

```bash
sudo snap install multipass
```

Create a Multipass VM named `anbox` (the default `systems.anbox.vm_name`), then
install and initialise the appliance. The appliance's bootstrap commands change
between releases, so follow the official tutorial for the exact, version-current
steps:

> **Anbox Cloud Appliance tutorial:** <https://anbox-cloud.io/docs>

The following sequence brings the appliance to the end state our code needs
(sizing is illustrative — scale to your GPU/CPU/RAM). It was verified with
appliance `1.30.0`; adapt if a newer release changes a step.

```bash
# 1. Create the VM. The 1.30 appliance needs LXD >= 5.21, newer than 22.04 ships,
#    so refresh LXD before initialising.
multipass launch --name anbox --cpus 8 --memory 16G --disk 2T 22.04
multipass exec anbox -- sudo snap refresh lxd --channel=5.21/stable

# 2. Install the appliance, attach Ubuntu Pro, and ENABLE the anbox-cloud
#    entitlement. `pro attach` does not always auto-enable it, and it must be
#    enabled *before* init or AMS gets no image-server credentials (401s later).
multipass exec anbox -- sudo snap install anbox-cloud-appliance
multipass exec anbox -- sudo pro attach <UBUNTU_PRO_TOKEN>
multipass exec anbox -- sudo pro enable anbox-cloud --assume-yes

# 3. Prepare the node (installs the binder_linux kernel module Android needs;
#    without it every instance fails "Failed to load kernel module binder_linux")
#    and initialise.
multipass exec anbox -- bash -c 'sudo anbox-cloud-appliance prepare-node-script | sudo bash -e'
multipass exec anbox -- sudo anbox-cloud-appliance init --auto
multipass exec anbox -- anbox-cloud-appliance status         # sanity-check -> status: ready

# 4. Add the Android 15 base image and wait for it to become active.
multipass exec anbox -- bash -c 'cd /tmp && sudo amc image add android15 jammy:android15:amd64 --type container --timeout 20m'
multipass exec anbox -- sudo amc image ls                    # jammy:android15:amd64 -> active
```

The control script launches `jammy:android15:amd64` with
`--enable-graphics --gpu-type nvidia --enable-streaming`. `--enable-streaming` is
required (anbox-connect bridges adb over the WebRTC stream). Multipass provides
no GPU passthrough, so the node has zero GPU slots; instances still reach
`running` (AMS falls back to software rendering), which is enough for the
provisioning/deploy metrics, but FPS is **not** GPU-accurate on this setup — run
FPS on a bare-metal appliance with a real GPU if you need it. Note also that
`jammy:android15:amd64` reports `ro.product.cpu.abilist = x86_64,x86` (no arm64
translation), so ARM-only corpus apps will not install on the Anbox baseline.

#### 3. Wire up our code

`anbox_test.sh` now resolves its own directory automatically (override with
`ANBOX_BASE_DIR`), so no source edits are needed.

#### 4. Verify

```bash
python -m mconbench run provision_concurrent --system anbox --config config/smoke.yaml
```

As our code brings containers up, `adb devices` should list `127.0.0.1:<port>`
entries (assigned dynamically by the gateway).

### GAE (Google Android Emulator)

Stock AVDs launched via the Android SDK emulator (adb port stride 2:
console + adb per instance). Our code drives them through
[`platform/avd.sh`](../platform/avd.sh) (`run|stop|rm`), which **auto-installs
the system image and auto-creates the AVDs** — so you only need to install the
SDK and enable KVM; no manual AVD creation is required.

#### 1. Host prerequisites

- KVM enabled and accessible (if you haven't already):
  ```bash
  sudo apt install -y qemu-kvm
  sudo adduser "$USER" kvm      # log out/in for the group to take effect
  test -w /dev/kvm && echo "KVM OK"
  ```
- An NVIDIA GPU + driver (≥550) for `-gpu host` rendering (the same requirement
  as MCon). On multi-GPU hosts `avd.sh` auto-selects a GPU via NVIDIA PRIME
  offload; override with `__NV_PRIME_RENDER_OFFLOAD` / `__GLX_VENDOR_LIBRARY_NAME`.

#### 2. Install the Android SDK

Download the "Command line tools" package from
<https://developer.android.com/studio#command-line-tools> and unpack it so it
lives at `cmdline-tools/latest`:

```bash
export ANDROID_SDK_ROOT="$HOME/Android/Sdk"
mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"
cd "$ANDROID_SDK_ROOT/cmdline-tools"
unzip ~/Downloads/commandlinetools-linux-*_latest.zip
mv cmdline-tools latest        # required layout: cmdline-tools/latest/bin/...

# Put the SDK tools on PATH (avd.sh also prepends the ~/Android/Sdk paths, but
# our code itself needs `adb` on PATH):
export PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/emulator:$PATH"

yes | sdkmanager --licenses
sdkmanager "platform-tools" "emulator"
```

`avd.sh` installs `system-images;android-34;google_apis;x86_64` on first run — a
Google APIs x86_64 image with **arm64 translation**
(`ro.product.cpu.abilist = x86_64,arm64-v8a`), so the corpus's ARM-only apps run.
We use the API 34 (Android 14) image rather than the paper's Android 13 (API 33)
because *every* Android 13 x86_64 image (`default`, `google_apis`, and
`google_apis_playstore`) dropped arm64 translation (`abilist = x86_64` only) and
cannot run the ARM-only apps; API 34 is the closest Android version whose Google
APIs image restores it. The emulated OS version does not affect the
provisioning / deploy / FPS trends. Pre-install it with
`sdkmanager "system-images;android-34;google_apis;x86_64"` to front-load the
download. It then creates `pixel_5` AVDs (2 GB RAM, 4 vCPU, 8 GB data,
1080×1920, Vulkan off) named `<avd_prefix>-1..N`.

#### 3. Point the repo at the SDK

If the SDK is not at the default `~/Android/Sdk`, set its location (either
works):

```bash
export ANDROID_SDK_ROOT=/path/to/Sdk      # in your .env / shell
# or config/default.yaml -> systems.gae.sdk_root: /path/to/Sdk
```

Relevant `systems.gae` keys (see [`config/default.yaml`](../config/default.yaml)):

| Key | Meaning |
|---|---|
| `base_adb_port` (5555) | emulator *i* → `localhost:(base_adb_port + 2*i)` |
| `base_console_port` (55554) | emulator console base (stride 2) |
| `avd_prefix` (`avd-batch`) | AVD name prefix; `rm` scope is `<prefix>-1..N` |
| `no_window` (true) | headless (`AVD_NO_WINDOW=1`); render offscreen like the paper |
| `sdk_root` | optional `ANDROID_SDK_ROOT` override |

> **Lifecycle to be aware of:** `avd.sh stop` kills *all* emulator processes on
> the host (fine when GAE is the only emulator running), and the cold-boot path
> deletes `<prefix>-1..N` before launch so each run recreates the AVD fresh.
> Don't run GAE alongside another emulator-based system on the same host.

#### 4. Verify

```bash
python -m mconbench run provision_concurrent --system gae --config config/smoke.yaml
```

You should see instances register as `emulator-55554`, `emulator-55556`, … (adb
addressed as `localhost:5555`, `localhost:5557`, … — console/adb stride 2) and a
canonical CSV under `data/runs/<timestamp>/`.

> **Headless GPU note:** `avd.sh` launches with `-gpu host`. On a machine with
> no GL-capable display this can fail; ensure the NVIDIA driver + an EGL/GBM
> path are present (as for MCon), or switch `-gpu host` to
> `-gpu swiftshader_indirect` in `avd.sh` for software rendering (lower FPS,
> fine for provisioning/deploy experiments).
