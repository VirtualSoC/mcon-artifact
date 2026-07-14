#!/usr/bin/env python3
"""
Container utilities for managing Android user/container mappings.
Provides functionality to store and retrieve PID to user ID mappings.
"""
import re
import shlex
import subprocess

# Global storage for PID-user mappings
# Format: {pid: user_id}
_pid_user_mapping = {}

def has_suffix(package: str) -> bool:
    """
    Checks if the package name has a user suffix.

    Args:
        package (str): The package name to check.

    Returns:
        bool: True if the package has a user suffix, False otherwise.
    """
    return bool(re.search(r"\.user\d+$", package))

def remove_suffix(package: str) -> str:
    """
    Removes the user suffix from the package name.

    Args:
        package (str): The package name from which to remove the suffix.

    Returns:
        str: The package name without the user suffix.
    """
    return re.sub(r"\.user\d+$", "", package)

def add_suffix(package: str, user_id: int) -> str:
    """
    Adds a user suffix to the package name.

    Args:
        package (str): The package name to which to add the suffix.
        user_id (int): The user ID to append.

    Returns:
        str: The package name with the user suffix added.
    """
    if user_id == 0 or has_suffix(package):
        return package
    return f"{package}.user{user_id}"

def get_user_display_mapping(device):
    """Return mapping from user ID to display ID for a specific device.

    Caches results per device serial so multiple devices don't overwrite each other.
    Use clear_user_display_cache(device) to invalidate a single device or
    clear_user_display_cache() to clear all.
    """
    # Initialize cache dict on first use
    if not hasattr(get_user_display_mapping, "user_display_cache") or not isinstance(get_user_display_mapping.user_display_cache, dict):
        get_user_display_mapping.user_display_cache = {}

    # Return cached per-device mapping if present
    cached = get_user_display_mapping.user_display_cache.get(device)
    if cached is not None:
        return cached

    try:
        # Local import to avoid circular import at module load time (adb_utils imports container_utils)
        from adb_utils import adb_shell  # type: ignore
    except Exception:
        adb_shell = None  # fallback if unavailable (should not happen)

    try:
        output = ""
        res = adb_shell(device, ["cmd", "container", "list", "running"], print_output=False)
        if res.get("returncode", 1) != 0:
            return {0: 0}
        output = res.get("stdout", "")

        user_display_map = {0: 0}  # Always map user 0 -> display 0
        pattern = r'UserInfo\[id=(\d+), name=CON-(\d+),'  # id/display pairs
        for line in output.strip().split('\n'):
            match = re.search(pattern, line)
            if match:
                user_id = int(match.group(1))
                display_id = int(match.group(2))
                user_display_map[user_id] = display_id

        get_user_display_mapping.user_display_cache[device] = user_display_map
        return user_display_map
    except Exception as e:
        print(f"Error getting user display mapping for {device}: {e}")
        return {0: 0}

def clear_user_display_cache(device: str = None):
    """Invalidate cached user->display mappings.

    Args:
        device: specific device serial; if None clear all cached entries.
    """
    if hasattr(get_user_display_mapping, "user_display_cache"):
        if device is None:
            get_user_display_mapping.user_display_cache.clear()
        else:
            get_user_display_mapping.user_display_cache.pop(device, None)

def query_all_users(device):
    """
    Queries all users on the Android device.

    Args:
        device: The serial number of the device or emulator.

    Returns:
        A list of user IDs on the device.
    """
    cache = get_user_display_mapping(device)
    return list(cache.keys())

def user_id_to_display_id(device, user_id, physical=True):
    """
    Convert user ID to display ID.
    
    Args:
        device: ADB device object
        user_id: User ID (int)
        
    Returns:
        int: Display ID, or None if not found
    """
    if user_id == 0:
        return 0
    mapping = get_user_display_mapping(device)
    display_id = mapping.get(user_id)
    if physical:
        return display_id - 1 if display_id and display_id > 0 else display_id
    return display_id 

def get_user_id_by_instance(instance_id):
    if instance_id < 0:
        raise ValueError("Instance ID must be non-negative.")
    if instance_id == 0:
        return 0
    else:
        return instance_id + 9

def get_instance_id(user_id):
    if user_id == 0:
        return 0
    if user_id < 10:
        raise ValueError("User ID must be 10 or greater for instance mapping.")
    else:
        return user_id - 9

def set_pid_user_mapping(pid, user_id):
    """
    Store the mapping of a PID to its user ID.
    
    Args:
        pid (str): Process ID as string
        user_id (int): Android user ID (-1 for unrecognized formats)
    """
    global _pid_user_mapping
    _pid_user_mapping[pid] = user_id

def get_user_id_by_pid(pid):
    """
    Retrieve the user ID associated with a given PID.
    
    Args:
        pid (str): Process ID as string
        
    Returns:
        int: User ID if found, -1 if not found or PID is invalid
    """
    global _pid_user_mapping
    return _pid_user_mapping.get(pid, -1)

def clear_pid_user_mapping():
    """
    Clear all stored PID-user mappings.
    Useful for starting fresh or cleaning up old data.
    """
    global _pid_user_mapping
    _pid_user_mapping.clear()

def get_pids_by_user(user_id):
    """
    Get all PIDs belonging to a specific user ID.
    
    Args:
        user_id (int): Android user ID to filter by
        
    Returns:
        list: List of PIDs (strings) for the specified user
    """
    global _pid_user_mapping
    
    pids = []
    for pid, proc_user_id in _pid_user_mapping.items():
        if proc_user_id == user_id:
            pids.append(pid)
    
    return pids


def get_pid_by_package_and_user(device, package_name, user_id):
    """
    Get all PID mappings belonging to a specific package name and user ID.
    
    Args:
        package_name (str): Package name to filter by
        user_id (int): Android user ID to filter by
        
    Returns:
        int: The PID of the specified package and user, or None if not found
    """
    from adb_utils import adb_shell  # Local import to avoid circular dependency
    # ps -o USER,PID,NAME -A | grep package_name | grep uXX_
    user_string = f"u{user_id}_"
    # Run the pipeline via a shell string so the pipe is interpreted by the remote shell.
    cmd = f"ps -o USER,PID,NAME -A | grep {shlex.quote(package_name)}"
    result = adb_shell(device, cmd, print_output=False)
    # parse output: uXX_axxx 1234 com.example.app
    output = result.get("stdout", "").strip()

    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and user_string in parts[0] and package_name in parts[2]:
            pid = parts[1]
            set_pid_user_mapping(pid, user_id)
            return int(pid)
    return None