import subprocess
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Dict, Iterable, Any

import container_utils
from container_utils import remove_suffix

excluded_system_packages = [
    "com.termux",
    "me.weishu.kernelsu",
    "xtr.keymapper",
    "com.amaze.filemanager",
    "net.sourceforge.opencamera",
    "me.zhanghai.android.files",
    "com.machiav3lli.fdroid",
]

def detect_adb(targets: Optional[Iterable[str]] = None,
                start: int = 5555,
                count: int = 64,
                timeout: float = 1.0) -> None:
    """Attempt `adb connect` on each provided target or scan a localhost range.

    Args:
        targets: Iterable of explicit serials/host:ports. If None, will iterate
                 localhost:<start>..localhost:<start+count-1>.
        start:   Starting port when scanning localhost range.
        count:   Number of consecutive ports to scan when targets is None.
        timeout: Per connect invocation timeout seconds.
    """
    if targets is None:
        targets = (f"localhost:{p}" for p in range(start, start + count))
    for t in targets:
        try:
            subprocess.run(["adb", "connect", t], capture_output=True, text=True, timeout=timeout)
        except Exception:
            pass

def get_adb_devices(return_first: bool = False) -> Optional[List[str]]:
    """Return list (or first) of devices currently visible to `adb devices`.

    Does NOT attempt any new connections; use `detect_adb` / `connect_local_range` first
    if you want to discover localhost instances.
    """
    try:
        res = subprocess.run(["adb", "devices"], capture_output=True, text=True)
        lines = res.stdout.strip().splitlines()[1:]
        serials: List[str] = []
        for ln in lines:
            if not ln.strip():
                continue
            parts = ln.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0].strip(), parts[1].strip()
            if state != "device":
                continue
            if serial.startswith("localhost:") or serial.startswith("127.0.0.1:"):
                serials.append(serial)
        def sort_key(s: str):
            if s.startswith("localhost:"):
                try:
                    return (0, int(s.split(":", 1)[1]))
                except ValueError:
                    return (1, s)
            if s.startswith("emulator-"):
                try:
                    return (1, int(s.split("-", 1)[1]))
                except ValueError:
                    return (1, s)
            return (2, s)
        serials.sort(key=sort_key)
        if return_first:
            return serials[0] if serials else None
        return serials
    except Exception as e:
        print(f"ADB detection error: {e}")
        return [] if not return_first else None


def get_user_installed_packages(device, user_id):
    """
    Queries all user-installed packages for a given user on the Android device.

    Args:
        device: The serial number of the device or emulator.
        user_id: The user ID for which to query the installed packages.
        Returns:
        A list of package names installed for the given user.
    """
    try:
        res = adb_shell(device, ["pm", "list", "packages", "--user", str(user_id), "-3"], print_output=False)
        if res["returncode"] != 0:
            print(f"[{device}] Error querying packages for user {user_id}: {res['stderr']}")
            return []
        packages = []
        for line in res["stdout"].splitlines():
            if line.startswith("package:"):
                packages.append(line.split("package:")[1].strip())
        packages = [p for p in packages if p not in excluded_system_packages]
        # remove suffix in package names
        packages = [remove_suffix(p) for p in packages]
        return packages
    except Exception as e:
        print(f"[{device}] ADB error: {e}")
        return []

def install_apk(device, user_id, apk_path):
    """
    Install an APK or all APKs under a directory for a specific user.

    Args:
        device: ADB serial of the device/emulator.
        user_id: Android user ID to target.
        apk_path: Path to a single .apk file or a directory containing .apk files.
    """
    path = Path(apk_path)
    if path.is_dir():
        package_files = sorted(
            p for p in path.rglob("*")
            if p.is_file() and p.suffix.lower() in {".apk", ".xapk"}
        )
        if not package_files:
            print(f"[{device}] No installable packages (.apk/.xapk) found under: {path}")
            return
        print(f"[{device}] Installing {len(package_files)} package(s) for user {user_id} from {path}...")
        ok = 0
        for pkg in package_files:
            if _install_single_apk(device, user_id, pkg):
                ok += 1
        print(f"[{device}] Done: {ok}/{len(package_files)} package(s) installed for user {user_id} from {path}")
        return
    if path.is_file():
        if path.suffix.lower() in {".apk", ".xapk"}:
            _install_single_apk(device, user_id, path)
        else:
            print(f"[{device}] Unsupported package type for installation: {path}")
        return
    print(f"[{device}] Path not found: {apk_path}")


def _install_single_apk(device: str, user_id: int, file_path) -> bool:
    """Install a single .apk or .xapk package. Returns True on success, False otherwise."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".apk":
        return _adb_install(device, user_id, path)
    if suffix == ".xapk":
        return _install_xapk_archive(device, user_id, path)

    print(f"[{device}] Unsupported package type: {path}")
    return False


def install_existing_package(device: str, user_id: int, package_name: str) -> bool:
    """Re-install an existing APK already on the device for the specified user.

    Args:
        device: ADB serial of the device/emulator.
        user_id: Android user ID to target.
        package_name: The package name of the app to re-install.
    Returns:
        True on success, False otherwise.
    """
    try:
        install_res = adb_shell(
            device,
            ["pm", "install-existing", "--user", str(user_id), package_name],
            print_output=False
        )
        if install_res["returncode"] == 0:
            print(f"[{device}] Installed existing package '{package_name}' for user {user_id}")
            return True
        print(f"[{device}] Error installing package '{package_name}' for user {user_id}: {install_res['stderr']}")
        return False
    except Exception as e:
        print(f"[{device}] ADB error: {e}")
        return False


def _adb_install(device: str, user_id: int, apk_path: Path) -> bool:
    """Run a standard adb install for a single APK."""
    try:
        result = subprocess.run(
            ["adb", "-s", device, "install", "--user", str(user_id), str(apk_path)],
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        print(f"[{device}] ADB error: {e}")
        return False

    if result.returncode == 0:
        print(f"[{device}] Installed for user {user_id}: {apk_path}")
        return True

    print(f"[{device}] Error installing for user {user_id}: {apk_path}")
    if result.stderr.strip():
        print(f"[{device}] {result.stderr.strip()}")
    return False


def _install_xapk_archive(device: str, user_id: int, archive_path: Path) -> bool:
    """Extract an .xapk archive and install its contained APK splits via adb install-multiple."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            with tempfile.TemporaryDirectory() as tmp_dir:
                zf.extractall(tmp_dir)
                extracted_apks = sorted(Path(tmp_dir).rglob("*.apk"))
                if not extracted_apks:
                    print(f"[{device}] No APK files found inside archive: {archive_path}")
                    return False

                ordered = sorted(extracted_apks, key=_xapk_sort_key)
                cmd = [
                    "adb", "-s", device, "install-multiple", "--user", str(user_id)
                ] + [str(p) for p in ordered]

                result = subprocess.run(cmd, capture_output=True, text=True)
    except zipfile.BadZipFile:
        print(f"[{device}] Invalid XAPK archive: {archive_path}")
        return False
    except subprocess.CalledProcessError as e:
        print(f"[{device}] ADB error: {e}")
        return False

    if result.returncode == 0:
        print(f"[{device}] Installed XAPK for user {user_id}: {archive_path}")
        return True

    print(f"[{device}] Error installing XAPK for user {user_id}: {archive_path}")
    if result.stderr.strip():
        print(f"[{device}] {result.stderr.strip()}")
    return False


def _xapk_sort_key(path: Path):
    """Sort key to install base APK before configuration splits."""
    name = path.name.lower()
    if name == "base.apk":
        return (0, name)
    if name.startswith("split_config"):
        return (1, name)
    return (2, name)

def get_main_activity_for_package(device, user_id, package_name):
    """
    Resolves the main activity for a given package.

    Args:
        device: The serial number of the device or emulator.
        user_id: The user ID for which to resolve the main activity.
        package_name: The name of the package.

    Returns:
        The full component name of the main activity (e.g., com.package/.MainActivity)
        or None if not found or an error occurs.
    """
    try:
        # Resolve activity (take last non-empty line)
        res = adb_shell(device, ["cmd", "package", "resolve-activity", "--user", str(user_id), "--brief", package_name], print_output=False)
        if res["returncode"] == 0:
            lines = [l.strip() for l in res["stdout"].splitlines() if l.strip()]
            if lines:
                activity_name = lines[-1]
                if '/' in activity_name:
                    return activity_name
        # Fallback: dump package info
        dump_res = adb_shell(device, ["pm", "dump", package_name], print_output=False)
        if dump_res["returncode"] == 0:
            main_activity_pattern = re.compile(r"android\.intent\.action\.MAIN:[\s\S]*?([\w\.]+/\.[\w\.]+)")
            match = main_activity_pattern.search(dump_res["stdout"])
            if match:
                return match.group(1)
        return None
    except Exception as e:
        print(f"[{device}] Unexpected error resolving main activity for {package_name}: {e}")
        return None

def start_activity(device, user_id, activity_name):
    """
    Starts the given activity for the specific user on the device.
    activity_name should be in the format: com.package.name/.ActivityName
    """
    display_id = container_utils.user_id_to_display_id(device, user_id, physical=False)
    if display_id is None:
        print(f"[{device}] Unable to determine display ID for user {user_id}")
        return
    res = adb_shell(device, ["am", "start", "--display", str(display_id), "--user", str(user_id), "-n", activity_name], print_output=False)
    if res["returncode"] == 0:
        print(f"[{device}] Activity started successfully for user {user_id}: {activity_name}")
    else:
        print(f"[{device}] Error starting activity for user {user_id}: {activity_name}")
        if res["stderr"].strip():
            print(f"[{device}] {res['stderr']}")

def start_package(dev, user_id, package_name):
    resolved_activity = get_main_activity_for_package(dev, user_id, container_utils.add_suffix(package_name, user_id))
    if not resolved_activity:
        resolved_activity = get_main_activity_for_package(dev, user_id, package_name)
    if resolved_activity:
        start_activity(dev, user_id, resolved_activity)
    else:
        print(f"[{dev}] Error: Could not resolve main activity for package '{package_name}' for user {user_id}.")

def return_home(device, user_id):
    """
    Returns to the home screen for the specific user on the device.
    """
    try:
        display_id = container_utils.user_id_to_display_id(device, user_id, physical=False)
        print(f"[{device}] Returning to home for user {user_id} on display {display_id}...")
        res = adb_shell(device, ["am", "start", "--user", str(user_id), "--display", str(display_id), "-a", "android.intent.action.MAIN", "-c", "android.intent.category.HOME"], print_output=False)
        if res["returncode"] != 0:
            print(f"[{device}] Error returning to home for user {user_id}")
            if res["stderr"].strip():
                print(f"[{device}] {res['stderr']}")
    except Exception as e:
        print(f"[{device}] ADB error: {e}")

def stop_package(device, package_name):
    """
    Stops the given package on the device.
    
    Args:
        device: The serial number of the device or emulator.
        package_name: The name of the package to stop.
    """
    try:
        res = adb_shell(device, ["am", "force-stop", package_name], print_output=False)
        if res["returncode"] != 0:
            print(f"[{device}] Error stopping package: {package_name}")
            if res["stderr"].strip():
                print(f"[{device}] {res['stderr']}")
    except Exception as e:
        print(f"[{device}] ADB error: {e}")

def clear_package_data(device, package_name):
    """
    Clears the data for the given package on the device.
    Args:
        device: The serial number of the device or emulator.
        package_name: The name of the package to clear data for.
    """
    try:
        res = adb_shell(device, ["pm", "clear", package_name], print_output=False)
        if res["returncode"] == 0:
            print(f"[{device}] Cleared data for package: {package_name}")
        else:
            print(f"[{device}] Error clearing data for package: {package_name}")
            if res["stderr"].strip():
                print(f"[{device}] {res['stderr']}")
    except Exception as e:
        print(f"[{device}] ADB error: {e}")

def adb_shell(
    device: str,
    shell_args,
    print_output: bool = False,
    timeout: float = None,
    async_: bool = False,
    **kwargs,
) -> Dict[str, Any]:
    """Execute an arbitrary adb shell command on a device with a timeout.

    Args:
        device: adb serial (e.g. localhost:5555)
        shell_args: list of arguments forming the command after 'adb shell'
        print_output: if True, print stdout/stderr labeled with device
        timeout: seconds before aborting the command (default None = no timeout)
    async_: if True, launch the command without waiting for completion and
         return a handle to the underlying process. Alias: pass
         {"async": True} via kwargs if you need the literal name.

    Returns:
        dict with keys: stdout, stderr, returncode (int)
            returncode -2 indicates timeout, -1 indicates other exception.
        When async_ is True, stdout/stderr will be empty strings, returncode
        will be None, and the dict will include a "process" key with the
        Popen handle.
    """
    # Allow passing either a list of args (recommended) or a single shell command string
    if isinstance(shell_args, str):
        cmd_line = ["adb", "-s", device, "shell", shell_args]
    else:
        cmd_line = ["adb", "-s", device, "shell"] + list(shell_args)
    if "async" in kwargs:
        async_alias = kwargs.pop("async")
        if async_ and async_alias:
            raise ValueError("async_ and async alias both set to True")
        async_ = async_alias
    if kwargs:
        unexpected = ", ".join(kwargs.keys())
        raise TypeError(f"adb_shell() got unexpected keyword arguments: {unexpected}")
    if async_:
        try:
            process = subprocess.Popen(
                cmd_line,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            if print_output:
                print(f"[{device}] Exception launching async shell command: {e}")
            return {"stdout": "", "stderr": str(e), "returncode": -1, "process": None}

        if print_output:
            print(f"[{device}] launched async shell command: {' '.join(cmd_line)}")
        return {"stdout": "", "stderr": "", "returncode": None, "process": process}

    try:
        result = subprocess.run(cmd_line, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        if print_output:
            print(f"[{device}] shell timeout after {timeout}s: {' '.join(cmd_line)}")
        return {
            "stdout": e.stdout or "",
            "stderr": e.stderr or f"TIMEOUT after {timeout}s",
            "returncode": -2,
            "process": None,
        }
    except Exception as e:
        if print_output:
            print(f"[{device}] Exception running shell command: {e}")
        return {"stdout": "", "stderr": str(e), "returncode": -1, "process": None}

    if print_output:
        if result.stdout.strip():
            print(f"[{device}] stdout: {result.stdout.rstrip()}")
        if result.stderr.strip():
            print(f"[{device}] stderr: {result.stderr.rstrip()}")
        if result.returncode != 0 and not result.stderr.strip():
            print(f"[{device}] shell command exited with code {result.returncode}")
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
        "process": None,
    }


def get_focused_windows(device: str) -> List[tuple]:
    """Return list of (user_id, app_package, activity) for focused windows on a device.

    Args:
        device: ADB serial of target emulator/device.
    """
    try:
        res = adb_shell(device, ["dumpsys", "activity", "activities"], print_output=False)
        if res["returncode"] != 0:
            return []
        focused_windows = []
        for line in res["stdout"].strip().split('\n'):
            if not line.strip():
                continue
            match = re.search(r'mFocusedWindow=Window\{[^}]+\s+u(\d+)\s+([^/]+)/([^}]+)\}', line)
            if match:
                user_id = int(match.group(1))
                app_package = remove_suffix(match.group(2))
                activity = match.group(3)
                focused_windows.append((user_id, app_package, activity))
        return focused_windows
    except Exception:
        return []


