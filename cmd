#!/usr/bin/env python3
import sys
import argparse
import time
import threading
import statistics
import os
import re
import subprocess
from queue import Queue
from pathlib import Path
from adb_utils import *
import adb_utils
import container_utils
import profiler
from container_utils import get_user_id_by_instance

LIST_USER_TIMEOUT = float(os.environ.get("LIST_USER_TIMEOUT", "2.0"))
LIST_USER_RETRIES = int(os.environ.get("LIST_USER_RETRIES", "3"))
LIST_USER_RETRY_DELAY = float(os.environ.get("LIST_USER_RETRY_DELAY", "0.5"))
MAX_INSTALL_RETRIES = 1
INSTALL_RETRY_SLEEP = float(os.environ.get("INSTALL_RETRY_SLEEP", "10"))
MAX_INSTALL_CONCURRENCY = max(1, int(os.environ.get("MAX_INSTALL_CONCURRENCY", "16")))

def list_users_with_timeout(dev, timeout=LIST_USER_TIMEOUT):
    for attempt in range(1, LIST_USER_RETRIES + 1):
        try:
            proc = subprocess.run(
                ["adb", "-s", dev, "shell", "cmd", "user", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            msg = f"[{dev}] list-user timed out (attempt {attempt}/{LIST_USER_RETRIES})"
            if attempt == LIST_USER_RETRIES:
                print(f"{msg}; skipping")
                return None
            print(f"{msg}; retrying...")
            time.sleep(LIST_USER_RETRY_DELAY)
            continue

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if stderr:
                print(f"[{dev}] list-user adb error (attempt {attempt}/{LIST_USER_RETRIES}): {stderr}")
            if attempt == LIST_USER_RETRIES:
                return None
            time.sleep(LIST_USER_RETRY_DELAY)
            continue

        matches = re.findall(r"UserInfo\{(\d+):", proc.stdout)
        return [int(m) for m in matches]

    return None


def format_epoch_ms(epoch):
    if epoch is None:
        return "-"
    try:
        secs = int(epoch)
    except (TypeError, ValueError):
        return "-"
    frac = epoch - secs
    if frac < 0:
        frac = 0.0
    ms = int(frac * 1000)
    try:
        base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(secs))
    except (OSError, ValueError, OverflowError):
        return "-"
    return f"{base}.{ms:03d}"

def create_parser():
    # Parent parser holding global options so they can appear before OR after subcommands
    common = argparse.ArgumentParser(add_help=False)
    # Use default=argparse.SUPPRESS so subparsers don't overwrite values parsed before the subcommand
    common.add_argument('-i', '--instance', default=argparse.SUPPRESS,
                        help='Target specific instance ID (default: all instances)')
    common.add_argument('-d', '--device', default=argparse.SUPPRESS,
                        help='Comma-separated list of device serials to operate on (e.g. localhost:5555,localhost:5556). If omitted, all detected devices are used.')

    parser = argparse.ArgumentParser(description='Android device management utility', parents=[common])
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Install command
    install_parser = subparsers.add_parser('install', help='Install APK to all users', parents=[common])
    install_parser.add_argument('apk_path', help='Path to APK file')
    
    # Start command
    start_parser = subparsers.add_parser('start', help='Start package on all users', parents=[common])
    start_parser.add_argument('package_name', help='Package name to start')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop package on all users', parents=[common])
    stop_parser.add_argument('package_name', help='Package name to stop')
    
    # Home command
    home_parser = subparsers.add_parser('home', help='Return to home screen for all users', parents=[common])

    # Clear data command
    clear_data_parser = subparsers.add_parser('clear-data', help='Clear data for package on all users', parents=[common])
    clear_data_parser.add_argument('package_name', help='Package name to clear data for')

    # List package command
    subparsers.add_parser('list-package', help='List packages for a user', parents=[common])
    
    # List user command
    subparsers.add_parser('list-user', help='List all users on device', parents=[common])
    
    # Profile command
    subparsers.add_parser('profile', help='Profile system stats', parents=[common])

    # Shell command (forward arbitrary command to adb shell)
    shell_parser = subparsers.add_parser('shell', help='Run arbitrary command via adb shell on each device', parents=[common])
    shell_parser.add_argument('shell_args', nargs=argparse.REMAINDER, help='Command to execute inside adb shell (use -- before command if needed)')

    return parser

def main():
    parser = create_parser
    args_parser = parser()
    args = args_parser.parse_args()
    
    if not args.command:
        args_parser.print_help()
        sys.exit(1)

    # Build device list (either user supplied or auto-detected)
    if hasattr(args, 'device') and args.device:
        requested = [s.strip() for s in args.device.split(',') if s.strip()]
        if not requested:
            print("No valid device serials provided via --device.")
            sys.exit(1)
        detect_adb(requested)
        listed = get_adb_devices() or []
        listed_set = set(listed)
        devices = [d for d in requested if d in listed_set]
        missing = [d for d in requested if d not in listed_set]
        if missing:
            print(f"Warning: requested devices not detected: {missing}")
        if not devices:
            print("None of the requested devices are available.")
            sys.exit(1)
    else:
        # Discover default localhost range then detect
        detect_adb()
        devices = get_adb_devices()
        if not devices:
            print("No ADB devices detected. Launch instances first.")
            sys.exit(1)

    print(f"Operating on {len(devices)} devices: {devices}")
    # Special-case 'install' to run installs in parallel across devices.
    if args.command == "install":
        apk_path = Path(args.apk_path)
        if not apk_path.exists():
            print(f"APK path not found: {apk_path}")
            sys.exit(1)
        if apk_path.is_dir():
            package_files = sorted(
                p for p in apk_path.rglob("*")
                if p.is_file() and p.suffix.lower() in {".apk", ".xapk"}
            )
            if not package_files:
                print(f"No installable packages (.apk/.xapk) found under: {apk_path}")
                sys.exit(1)
        else:
            package_files = [apk_path]

        base_dir = os.path.dirname(__file__)
        log_path = os.path.join(base_dir, "install_times.log")
        with open(log_path, "a") as lf:
            lf.write("Installation started at " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + " for " + str(devices) + "\n")

        device_users = {}
        for dev in devices:
            target_users = [get_user_id_by_instance(int(args.instance))] if hasattr(args, 'instance') and args.instance else container_utils.query_all_users(dev)
            if not target_users:
                print(f"[{dev}] No users resolved; skipping device")
                continue
            device_users[dev] = target_users

        if not device_users:
            print("No eligible devices with resolved users; aborting install")
            return

        device_stats = {
            dev: {"samples": [], "start_times": [], "end_times": []}
            for dev in device_users
        }

        def install_package_for_device(dev, pkg):
            target_users = device_users.get(dev, [])
            if not target_users:
                return
            name = pkg.name if hasattr(pkg, 'name') else str(pkg)
            for user_id in target_users:
                final_start = None
                final_end = None
                final_elapsed = 0.0
                success = False
                attempt_start = time.time()
                ok = False
                try:
                    ok = adb_utils._install_single_apk(dev, user_id, pkg)
                except Exception as e:
                    print(f"[{dev}] Exception installing {pkg}: {e}")
                attempt_end = time.time()
                attempt_elapsed = attempt_end - attempt_start
                final_start = attempt_start
                final_end = attempt_end
                final_elapsed = attempt_elapsed
                if ok:
                    success = True
                    print(f"[{dev}] {name} user={user_id} install_time={attempt_elapsed:.3f}s ok=True")
                else:
                    print(f"[{dev}] {name} user={user_id} install failed")
                if final_start is None:
                    final_start = time.time()
                    final_end = final_start
                    final_elapsed = 0.0
                if success:
                    device_stats[dev]["samples"].append(final_elapsed)
                else:
                    device_stats[dev]["samples"].append(-1.0)
                    print(f"[{dev}] {name} user={user_id} install_time=-1s ok=False (all attempts exhausted)")
                device_stats[dev]["start_times"].append(final_start)
                device_stats[dev]["end_times"].append(final_end)

        ordered_devices = list(device_users.keys())
        device_progress = {dev: 0 for dev in ordered_devices}
        task_queue: Queue = Queue()
        for dev in ordered_devices:
            task_queue.put(dev)

        def worker():
            while True:
                dev = task_queue.get()
                if dev is None:
                    task_queue.task_done()
                    break
                try:
                    pkg_index = device_progress.get(dev, 0)
                    if pkg_index >= len(package_files):
                        continue
                    pkg = package_files[pkg_index]
                    pkg_name = pkg.name if hasattr(pkg, 'name') else str(pkg)
                    print(f"[host] {dev} installing {pkg_name} (slot {pkg_index+1}/{len(package_files)})")
                    install_package_for_device(dev, pkg)
                    device_progress[dev] = pkg_index + 1
                    if device_progress[dev] < len(package_files):
                        task_queue.put(dev)
                finally:
                    task_queue.task_done()

        workers = []
        thread_count = min(MAX_INSTALL_CONCURRENCY, len(ordered_devices))
        for _ in range(thread_count):
            t = threading.Thread(target=worker, daemon=False)
            t.start()
            workers.append(t)

        task_queue.join()
        for _ in workers:
            task_queue.put(None)
        for t in workers:
            t.join()

        with open(log_path, "a") as lf:
            for dev in ordered_devices:
                stats = device_stats.get(dev)
                if not stats or not stats["samples"]:
                    continue
                successful = [t for t in stats["samples"] if t >= 0]
                mean = statistics.mean(successful) if successful else 0.0
                sd = statistics.stdev(successful) if len(successful) > 1 else 0.0
                times_str = ",".join(f"{t:.3f}" if t >= 0 else "-1" for t in stats["samples"])
                start_times_str = ",".join(format_epoch_ms(ts) for ts in stats["start_times"])
                end_times_str = ",".join(format_epoch_ms(ts) for ts in stats["end_times"])
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                line = (
                    f"{ts},{dev},mean={mean:.3f},sd={sd:.3f},"
                    f"start_times=[{start_times_str}],end_times=[{end_times_str}],times=[{times_str}]\n"
                )
                print(line.strip())
                lf.write(line)

        print(f"Installation timing logs appended to {log_path}")
        return

    # For all other commands, operate per-device (devices processed serially here)
    overall_rc = 0
    for dev in devices:
        if args.command == "list-user":
            users = list_users_with_timeout(dev)
            if users is None:
                continue
            if users:
                print(f"[{dev}] users: {users}")
            else:
                print(f"[{dev}] No users found")
            continue

        target_users = [get_user_id_by_instance(int(args.instance))] if hasattr(args, 'instance') and args.instance else container_utils.query_all_users(dev)

        if args.command == "start":
            for user_id in target_users:
                time.sleep(1)
                start_package(dev, user_id, args.package_name)
        elif args.command == "stop":
            for user_id in target_users:
                stop_package(dev, container_utils.add_suffix(args.package_name, user_id))
        elif args.command == "home":
            for user_id in target_users:
                return_home(dev, user_id)
        elif args.command == "clear-data":
            for user_id in target_users:
                clear_package_data(dev, container_utils.add_suffix(args.package_name, user_id))
        elif args.command == "list-package":
            if not target_users:
                print(f"[{dev}] No users found")
                continue
            packages = get_user_installed_packages(dev, target_users[0])
            print(f"[{dev}] Packages for user {target_users[0]}:")
            if packages:
                for package in packages:
                    print(f"  {package}")
            else:
                print("  (none)")
        elif args.command == "profile":
            print(f"[{dev}] profiling...")
            profiler.get_all_system_stats(dev)
        elif args.command == "shell":
            if not args.shell_args:
                print("Error: shell command requires arguments (e.g. shell getprop ro.build.version.release)")
                break
            res = adb_shell(dev, args.shell_args, print_output=True)
            rc = res.get("returncode", 0) if isinstance(res, dict) else int(res)
            overall_rc = rc
            if rc != 0:
                break

    if overall_rc != 0:
        sys.exit(overall_rc)

if __name__ == "__main__":
    main()
