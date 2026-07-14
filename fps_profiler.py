import re
import subprocess
from collections import defaultdict
from adb_utils import *  # provides adb_shell
from container_utils import get_instance_id, remove_suffix, get_user_id_by_pid


def get_unique_apps_by_user(focused_windows):
    """
    Group focused windows by app package and collect users running each app.
    
    Args:
        focused_windows: List of (user_id, app_package, activity) tuples
        
    Returns:
        dict: Dictionary mapping app_package to list of user_ids running it
    """
    apps_by_users = defaultdict(set)
    
    for user_id, app_package, activity in focused_windows:
        apps_by_users[app_package].add(user_id)
    
    # Convert sets to sorted lists for consistent output
    return {app: sorted(list(users)) for app, users in apps_by_users.items()}

def measure_app_fps(device, identifier):
    """Measure FPS for all instances of an app using centralized adb_shell.

    Args:
        device: adb serial
        identifier: package name or PID

    Returns:
        dict mapping PID -> parsed gfxinfo metrics
    """
    try:
        res = adb_shell(device, ["dumpsys", "gfxinfo", str(identifier)], print_output=False, timeout=10)
        if res.get("returncode", 1) != 0:
            # Non-zero exit: treat as no data
            if res.get("stderr"):
                if "No process found for" in res.get("stderr", ""):
                    return {}
            return {}
        stdout = res.get("stdout", "")
        if "No process found for" in stdout or not stdout.strip():
            print(f"No graphics info available for {identifier}")
            return {}
        # print(f"Debug: gfxinfo output for PID {identifier}:\n{stdout}\n")
        return parse_multi_user_gfxinfo(stdout)
    except Exception as e:
        print(f"Error measuring FPS for {identifier}: {e}")
        return {}

def parse_gfxinfo_section(section_text, pid):
    """
    Parse a single graphics info section and extract histogram data.
    
    Args:
        section_text (str): The graphics info section for one PID
        pid (str): Process ID for this section
        
    Returns:
        dict: Contains histogram data and calculated metrics
    """
    try:
        histogram_match = re.search(r"HISTOGRAM: (.*?)(?=50th gpu percentile|GPU HISTOGRAM|$)", section_text, re.DOTALL)
        if not histogram_match:
            return None
            
        histogram_text = histogram_match.group(1)
        histogram_entries = re.findall(r"(\d+)ms=(\d+)", histogram_text)
        histogram_dict = {int(ms): int(value) for ms, value in histogram_entries}
        
        if not histogram_dict:
            return None
            
        total_frames = sum(histogram_dict.values())
        if total_frames == 0:
            return None
            
        weighted_sum = sum(ms * count for ms, count in histogram_dict.items())
        average_latency = weighted_sum / total_frames
        fps = 1000 / average_latency if average_latency > 0 else 0
        
        # Extract additional metrics
        percentile_50_match = re.search(r"50th percentile: (\d+)ms", section_text)
        percentile_90_match = re.search(r"90th percentile: (\d+)ms", section_text)
        percentile_95_match = re.search(r"95th percentile: (\d+)ms", section_text)
        
        return {
            'pid': pid,
            'histogram': histogram_dict,
            'total_frames': total_frames,
            'average_latency': average_latency,
            'fps': fps,
            'percentile_50': int(percentile_50_match.group(1)) if percentile_50_match else None,
            'percentile_90': int(percentile_90_match.group(1)) if percentile_90_match else None,
            'percentile_95': int(percentile_95_match.group(1)) if percentile_95_match else None
        }
    except Exception as e:
        print(f"Error parsing graphics info for PID {pid}: {e}")
        return None

def parse_multi_user_gfxinfo(gfxinfo_output):
    """
    Parse gfxinfo output that may contain multiple user instances of the same app.
    
    Args:
        gfxinfo_output (str): Raw dumpsys gfxinfo output
        
    Returns:
        dict: Dictionary mapping PID to graphics info data
    """
    # Split the output into sections for each PID
    sections = re.split(r'\*\* Graphics info for pid (\d+) \[([^\]]+)\] \*\*', gfxinfo_output)
    
    results = {}
    
    # Process each section (skip the first empty section)
    for i in range(1, len(sections), 3):
        if i + 2 < len(sections):
            pid = sections[i]
            app_name = sections[i + 1]
            section_content = sections[i + 2]
            
            # Parse this section
            parsed_data = parse_gfxinfo_section(section_content, pid)
            if parsed_data:
                # Get user ID from container utils
                user_id = get_user_id_by_pid(pid)
                if user_id is None:
                    user_id = -1  # Unknown user
                    
                parsed_data['user_id'] = user_id
                parsed_data['app_name'] = app_name
                results[pid] = parsed_data
                
    return results

# Function to run the test
def run_automated_fps_test(device):
    """
    Automatically detect and measure FPS for topmost apps in each display.
    """

    print("FPS Summary (focused apps)\n")
    
    # Get focused windows information
    focused_windows = get_focused_windows(device)
    
    if not focused_windows:
        print("No focused windows found. Make sure displays are active.")
        return
    
    # Group by app package
    apps_by_users = get_unique_apps_by_user(focused_windows)
    
    print(f"Found {len(focused_windows)} active display(s) with {len(apps_by_users)} unique app(s):")
    
    for app_package, user_ids in apps_by_users.items():
        user_list = ", ".join([f"u{uid}" for uid in user_ids])
        print(f"  {app_package} (users: {user_list})")
    
    # Measure FPS for each unique app
    all_results = {}
    
    for app_package, expected_users in apps_by_users.items():
        app_results = measure_app_fps(device, app_package)
        
        if not app_results:
            print(f"No graphics info available for {app_package}")
            continue
            
        all_results[app_package] = app_results
        
        for pid, data in app_results.items():
            user_id = data['user_id']
            user_display = f"user {user_id}" if user_id != -1 else "unknown user"
            
            # Only show focused instances
            if user_id in expected_users:
                pass  # Details will be shown in summary report
    
    # Generate summary report
    
    focused_instances = []
    
    for app_package, app_results in all_results.items():
        expected_users = apps_by_users[app_package]
        
        for pid, data in app_results.items():
            user_id = data['user_id']
            # Only include focused instances
            if user_id in expected_users:
                instance_info = {
                    'app': app_package,
                    'user_id': user_id,
                    'pid': pid,
                    'fps': data['fps'],
                    'total_frames': data['total_frames']
                }
                focused_instances.append(instance_info)
    
    if focused_instances:
        avg_focused_fps = sum(inst['fps'] for inst in focused_instances) / len(focused_instances)
        print(f"\nFocused apps ({len(focused_instances)} instances): avg FPS {avg_focused_fps:.1f}")
        focused_instances.sort(key=lambda x: x['user_id'], reverse=False)
        
        for instance in focused_instances:
            print(f"  Instance {get_instance_id(instance['user_id'])}: {instance['app']} - "
                  f"FPS: {instance['fps']:.1f}, "
                  f"Frames: {instance['total_frames']}")
        
        avg_focused_fps = sum(inst['fps'] for inst in focused_instances) / len(focused_instances)
    else:
        print("\nNo focused app instances found.")

if __name__ == "__main__":
    # Attempt to discover local range then pick first available device
    detect_adb()
    device = get_adb_devices(return_first=True)
    if not device:
        print("Failed to connect to ADB device. Please ensure a device is connected and authorized.")
        exit(1)

    run_automated_fps_test(device)