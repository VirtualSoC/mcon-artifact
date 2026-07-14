#!/usr/bin/env python3
import subprocess
import re
import time
import sys
import telnetlib
from adb_utils import detect_adb, get_adb_devices
from container_utils import set_pid_user_mapping
from fps_profiler import run_automated_fps_test

def get_guest_cpu_memory_usage(device):
    """
    Get CPU and memory usage from Android guest using top command.
    
    Args:
        device: ADB device identifier
        
    Returns:
        tuple: (cpu_usage_dict, memory_usage_dict) where keys are PIDs/process names
               and values are usage percentages/MB respectively
    """
    try:
        result = subprocess.run([
            'adb', '-s', device, 'shell', 
            'top', '-n', '1', '-b', '-q', '-o', 'pid,user,res,%cpu,args'
        ], capture_output=True, text=True, timeout=1)
        
        if result.returncode != 0:
            return {}, {}
            
        cpu_usage = {}
        memory_usage = {}
        kernel_cpu = 0.0
        kernel_memory = 0.0
        
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
                
            # Split by whitespace, but limit splits to handle process names with spaces
            parts = line.split(None, 4)  # Split into max 5 parts: pid, user, res, cpu, args
            
            if len(parts) < 4:
                continue
                
            try:
                pid = parts[0]
                user = parts[1]
                memory_str = parts[2]  # e.g., "173M" or "6.7M"
                cpu_str = parts[3]     # e.g., "0.0" or "11.5"
                process_name = parts[4] if len(parts) > 4 else f"pid_{pid}"
                
                # Skip if PID is not numeric (header lines, etc.)
                if not pid.isdigit():
                    continue
                
                # Parse user ID from user field (e.g., u18_a244 -> user 18, u0_a482 -> user 0)
                user_id = 0  # Default for unrecognized formats
                if user.startswith('u') and '_' in user:
                    try:
                        user_id_str = user.split('_')[0][1:]  # Remove 'u' prefix and take part before '_'
                        user_id = int(user_id_str)
                    except (ValueError, IndexError):
                        user_id = 0  # Keep default for invalid formats

                # Parse CPU percentage
                cpu_percent = float(cpu_str)

                # Parse memory (handle 'M' suffix and convert to MB)
                if memory_str.endswith('M'):
                    memory_mb = float(memory_str[:-1])
                elif memory_str.isdigit():
                    memory_mb = float(memory_str) / 1024.0  # Assume KB if no suffix
                else:
                    memory_mb = 0.0

                # Categorize kernel threads vs user processes
                if process_name.startswith('[') and process_name.endswith(']'):
                    # Kernel thread
                    kernel_cpu += cpu_percent
                    kernel_memory += memory_mb
                else:
                    # User process - only store if there's meaningful usage
                    if cpu_percent > 0.0:
                        cpu_usage[process_name] = cpu_percent
                    if memory_mb > 0.0:
                        memory_usage[process_name] = memory_mb

                    # Store PID and user ID mapping in container utils
                    set_pid_user_mapping(pid, user_id)

            except (ValueError, IndexError) as e:
                # Skip malformed lines
                continue
        
        # Add kernel totals
        if kernel_cpu > 0.0:
            cpu_usage['[kernel]'] = kernel_cpu
        if kernel_memory > 0.0:
            memory_usage['[kernel]'] = kernel_memory

        # Remove the top command itself from results if present
        top_cmd = 'top -n 1 -b -q -o pid,user,res,%cpu,args'
        cpu_usage.pop(top_cmd, None)
        memory_usage.pop(top_cmd, None)

        return cpu_usage, memory_usage
        
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        print(f"Warning: Failed to get guest CPU/memory usage: {e}")
        return {}, {}

def calculate_average(data, total_measurements):
    avg_data = {pid: sum(usage_list) / total_measurements for pid, usage_list in data.items()}
    return avg_data

def get_qemu_process_usage():
    """
    Measure CPU and memory usage of QEMU-related processes on the host system.
    Includes both the main 'qemu-system-x86_64' and any 'vsoc-worker' subprocesses.
    Returns a dictionary with total CPU percentage and total memory usage in MB, or None.
    """
    try:
        # Collect PIDs for main qemu and vsoc-worker processes
        pids = set()
        for pattern in ('qemu-system-x86_64', 'vsoc-worker'):
            res = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                pids.update(pid for pid in res.stdout.strip().split('\n') if pid.strip())
        if not pids:
            return None
        
        qemu_pids = sorted(pid for pid in pids)
        total_cpu_percent = 0.0
        total_memory_mb = 0.0

        total_mem_kb = 0
        try:
            mem_info_result = subprocess.run(['cat', '/proc/meminfo'],
                                             capture_output=True, text=True, check=True)
            for mem_line in mem_info_result.stdout.split('\n'):
                if mem_line.startswith('MemTotal:'):
                    parts = mem_line.split()
                    if len(parts) >= 2:
                        total_mem_kb = int(parts[1])
                    break
        except Exception as e:  # Minor issue, can proceed
            print(f"Warning: Could not read total system memory: {e}")
            # total_mem_kb will be 0, affecting mem_mb calculation if %MEM is used without RES

        pids_processed = 0
        try:
            # Use one 'ps' invocation for all PIDs: ps -p pid1,pid2 -o pid,%cpu,%mem,cmd --no-headers
            ps_cmd = ['ps', '-p', ','.join(qemu_pids), '-o', 'pid,%cpu,%mem,cmd', '--no-headers']
            ps_result = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)
            pid_set = set(qemu_pids)
            for raw in ps_result.stdout.split('\n'):
                line = raw.strip()
                if not line:
                    continue
                # Split into at most 4 parts so CMD (which can contain spaces) remains intact
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                pid, cpu_str, mem_str, _cmd = parts
                if pid not in pid_set or not pid.isdigit():
                    continue
                try:
                    cpu_percent = float(cpu_str)
                    mem_percent = float(mem_str)
                except ValueError:
                    continue
                total_cpu_percent += cpu_percent
                if total_mem_kb > 0:
                    total_memory_mb += (mem_percent / 100.0) * (total_mem_kb / 1024.0)
                pids_processed += 1
        except Exception as e:
            print(f"Warning: Could not process QEMU PIDs via ps: {e}")

        if pids_processed > 0:
            return {'qemu_cpu_percent': total_cpu_percent, 'qemu_mem_mb': total_memory_mb}
        return None
        
    except Exception as e:
        print(f"Error measuring QEMU process usage: {e}")
        return None

def get_gpu_stats_for_qemu():
    """
    Get GPU utilization, total GPU memory usage, and QEMU-specific GPU memory usage.
    Includes memory used by both 'qemu-system-x86_64' and 'vsoc-worker' processes.
    Returns a dictionary with overall GPU utilization, total GPU memory, and QEMU GPU memory, or None.
    """
    try:
        # 1. Get QEMU-related PIDs (main + vsoc-worker)
        qemu_pids = set()
        for pattern in ('qemu-system-x86_64', 'vsoc-worker'):
            res = subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                qemu_pids.update(pid for pid in res.stdout.strip().split('\n') if pid.strip())

        # 2. Get overall GPU utilization and total memory usage (for the first/primary GPU)
        gpu_query_result = subprocess.run([
            'nvidia-smi',
            '--query-gpu=utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader,nounits'
        ], capture_output=True, text=True)
        
        overall_gpu_util = None
        gpu_mem_used_mb = None
        gpu_mem_total_mb = None
        
        if gpu_query_result.returncode == 0 and gpu_query_result.stdout.strip():
            try:
                # Take the first line for the first GPU
                values = gpu_query_result.stdout.strip().split('\n')[0].split(', ')
                if len(values) >= 3:
                    overall_gpu_util = float(values[0])
                    gpu_mem_used_mb = float(values[1])  # nvidia-smi returns MiB
                    gpu_mem_total_mb = float(values[2])  # nvidia-smi returns MiB
            except (ValueError, IndexError) as e:
                print(f"Warning: Could not parse GPU stats: {e}")
                overall_gpu_util = None
                gpu_mem_used_mb = None
                gpu_mem_total_mb = None
        
        # 3. Get GPU memory used by QEMU graphics processes
        qemu_gpu_mem_used_mb = 0
        if qemu_pids:
            # Parse the full nvidia-smi output for graphics processes
            full_smi_result = subprocess.run(['nvidia-smi'], capture_output=True, text=True)
            if full_smi_result.returncode == 0:
                lines = full_smi_result.stdout.split('\n')
                in_processes_section = False
                
                for line in lines:
                    # Look for the processes section
                    if '| Processes:' in line:
                        in_processes_section = True
                        continue
                    elif in_processes_section and line.strip().startswith('+'):
                        # End of processes section
                        break
                    elif in_processes_section and '|' in line and 'MiB' in line:
                        # More robust parsing: look for PID and memory in the line
                        # Example: |    0   N/A  N/A         1033972      G   bin/qemu-system-x86_64                 3534MiB |
                        try:
                            # Remove outer pipes and split by remaining pipes
                            cleaned_line = line.strip('| \n')
                            # Look for pattern: numbers followed by process name and memory
                            parts = cleaned_line.split()
                            
                            # Find PID (should be a number) and memory (should end with MiB)
                            pid_found = None
                            mem_found = None
                            
                            for i, part in enumerate(parts):
                                # Look for PID (numeric value that could be in our qemu_pids set)
                                if part.isdigit() and part in qemu_pids:
                                    pid_found = part
                                # Look for memory (ends with MiB)
                                elif 'MiB' in part:
                                    try:
                                        mem_value = float(part.replace('MiB', ''))
                                        mem_found = mem_value
                                    except ValueError:
                                        continue
                            
                            if pid_found and mem_found:
                                qemu_gpu_mem_used_mb += mem_found
                                
                        except Exception as e:
                            continue
                
        # Only return data if we have at least overall utilization, total memory, or qemu specific memory
        if (overall_gpu_util is not None or gpu_mem_used_mb is not None or 
            qemu_gpu_mem_used_mb > 0 or qemu_pids): # also return if qemu_pids were found, even if mem is 0
            return {
                'gpu_util_percent': overall_gpu_util, # This is overall GPU util
                'gpu_mem_used_mb': gpu_mem_used_mb, # Total GPU memory used
                'gpu_mem_total_mb': gpu_mem_total_mb, # Total GPU memory available
                'qemu_gpu_mem_used_mb': qemu_gpu_mem_used_mb # QEMU-specific GPU memory
            }
        return None # No useful GPU data obtained

    except FileNotFoundError:
        # print("nvidia-smi not found. Make sure NVIDIA drivers are installed.")
        return None
    except Exception as e:
        print(f"Error getting GPU stats for QEMU: {e}")
        return None

# Global telnet connection cache for QEMU monitor
_qemu_monitor_connection = None

def get_qemu_monitor_connection():
    """
    Get or create a cached telnet connection to QEMU monitor.
    Returns the connection object or None if unavailable.
    """
    global _qemu_monitor_connection
    
    try:
        # Check if existing connection is still alive
        if _qemu_monitor_connection is not None:
            try:
                # Test the connection by reading any pending data (non-blocking)
                _qemu_monitor_connection.read_very_eager()
                return _qemu_monitor_connection
            except:
                # Connection is dead, close it and create a new one
                try:
                    _qemu_monitor_connection.close()
                except:
                    pass
                _qemu_monitor_connection = None
        
        # Create new connection
        _qemu_monitor_connection = telnetlib.Telnet('localhost', 55555, timeout=2)
        # Read initial prompt to clear buffer
        _qemu_monitor_connection.read_until(b"(qemu)", timeout=2)
        return _qemu_monitor_connection
        
    except Exception as e:
        _qemu_monitor_connection = None
        return None

def close_qemu_monitor_connection():
    """
    Close the cached QEMU monitor connection.
    """
    global _qemu_monitor_connection
    if _qemu_monitor_connection is not None:
        try:
            _qemu_monitor_connection.close()
        except:
            pass
        _qemu_monitor_connection = None

def get_qemu_display_fps():
    """
    Get QEMU display framerate via telnet connection to QEMU monitor.
    Returns a dictionary with display FPS data or None if unavailable.
    """
    try:
        # Get cached connection
        tn = get_qemu_monitor_connection()
        if tn is None:
            return None
        
        # Send the display fps command
        tn.write(b"vsoc display fps\n")
        
        # Read the response
        response = tn.read_until(b"(qemu)", timeout=2).decode('utf-8')
        
        # Parse the FPS values from the response
        # Look for lines containing floating point numbers
        fps_values = []
        for line in response.split('\n'):
            line = line.strip()
            if line and not line.startswith('(qemu)') and not line.startswith('vsoc'):
                # Extract floating point numbers from the line
                fps_numbers = re.findall(r'\d+\.\d+', line)
                fps_values.extend([float(fps) for fps in fps_numbers])
        
        if fps_values:
            return {
                'display_fps_values': fps_values,
                'display_fps_count': len(fps_values),
                'display_fps_avg': sum(fps_values) / len(fps_values),
                'display_fps_min': min(fps_values),
                'display_fps_max': max(fps_values)
            }
        
        return None
        
    except Exception as e:
        # Connection might be broken, close it so next call will recreate
        close_qemu_monitor_connection()
        return None

def get_current_system_stats(device):
    """
    Collects a single snapshot of QEMU CPU/memory, GPU, and Android guest usage.
    Returns a flat dictionary containing all current stats.
    Keys are prefixed like 'qemu_', 'gpu_', 'guest_'.
    """
    flat_stats = {
        'qemu_cpu_percent': None,
        'qemu_mem_mb': None,
        'gpu_util_percent': None, # Overall GPU utilization
        'gpu_mem_used_mb': None, # Total GPU memory used
        'gpu_mem_total_mb': None, # Total GPU memory available
        'qemu_gpu_mem_used_mb': None, # QEMU-specific GPU memory
        'qemu_display_fps_avg': None, # QEMU display average FPS
        'qemu_display_fps_count': None, # Number of displays
        'qemu_display_fps_min': None, # Minimum display FPS
        'qemu_display_fps_max': None, # Maximum display FPS
        'guest_cpu_by_pid': None,
        'guest_mem_by_pid': None
    }

    # Get QEMU process usage (Host CPU/RAM)
    qemu_host_data = get_qemu_process_usage()
    if qemu_host_data:
        flat_stats['qemu_cpu_percent'] = qemu_host_data['qemu_cpu_percent']
        flat_stats['qemu_mem_mb'] = qemu_host_data['qemu_mem_mb']

    # Get GPU stats (Overall Util + Total GPU Memory + QEMU GPU Memory)
    gpu_data = get_gpu_stats_for_qemu()
    if gpu_data:
        flat_stats['gpu_util_percent'] = gpu_data['gpu_util_percent']
        flat_stats['gpu_mem_used_mb'] = gpu_data['gpu_mem_used_mb']
        flat_stats['gpu_mem_total_mb'] = gpu_data['gpu_mem_total_mb']
        flat_stats['qemu_gpu_mem_used_mb'] = gpu_data['qemu_gpu_mem_used_mb']

    # Get QEMU display FPS
    display_fps_data = get_qemu_display_fps()
    if display_fps_data:
        flat_stats['qemu_display_fps_avg'] = display_fps_data['display_fps_avg']
        flat_stats['qemu_display_fps_count'] = display_fps_data['display_fps_count']
        flat_stats['qemu_display_fps_min'] = display_fps_data['display_fps_min']
        flat_stats['qemu_display_fps_max'] = display_fps_data['display_fps_max']
        
    # Get guest usage (Guest CPU/RAM via ADB)
    # try:
    #     cpu_usage, memory_usage = get_guest_cpu_memory_usage(device)
    #     # Ensure cpu_usage and memory_usage are not None and are actual dictionaries
    #     if isinstance(cpu_usage, dict) and isinstance(memory_usage, dict):
    #         flat_stats['guest_cpu_by_pid'] = cpu_usage
    #         flat_stats['guest_mem_by_pid'] = memory_usage
    #     # else: guest stats remain None if adb fails or returns unexpected data
    # except Exception as e:
    #     pass
        
    return flat_stats

def print_final_statistics(device, sample_count, qemu_cpu_samples, qemu_mem_samples, gpu_util_samples, 
                          gpu_mem_used_samples, gpu_mem_total_samples, qemu_gpu_mem_samples,
                          qemu_display_fps_samples, qemu_display_count_samples,
                          accumulated_guest_cpu_data, accumulated_guest_mem_data, successful_guest_measurements):
    """
    Print final aggregated statistics
    """
    # --- Final Statistics Aggregation and Printing --- 
    print("\n" + "=" * 80)
    print("FINAL STATISTICS")
    print("=" * 80)
    
    final_aggregated_stats = {'samples_collected': sample_count}
    
    # QEMU aggregated statistics
    if qemu_cpu_samples: 
        final_aggregated_stats['qemu_cpu'] = {
            'average': sum(qemu_cpu_samples) / len(qemu_cpu_samples),
            'maximum': max(qemu_cpu_samples),
            'minimum': min(qemu_cpu_samples)
        }
        final_aggregated_stats['qemu_mem_mb'] = {
            'average': sum(qemu_mem_samples) / len(qemu_mem_samples),
            'maximum': max(qemu_mem_samples),
            'minimum': min(qemu_mem_samples)
        }
        print(f"\nHost QEMU (Aggregated over {len(qemu_cpu_samples)} samples):")
        print(f"  CPU Usage    - Avg: {final_aggregated_stats['qemu_cpu']['average']:5.1f}%, Max: {final_aggregated_stats['qemu_cpu']['maximum']:5.1f}%, Min: {final_aggregated_stats['qemu_cpu']['minimum']:5.1f}%")
        print(f"  Memory Usage - Avg: {final_aggregated_stats['qemu_mem_mb']['average']:7.1f} MB, Max: {final_aggregated_stats['qemu_mem_mb']['maximum']:7.1f} MB, Min: {final_aggregated_stats['qemu_mem_mb']['minimum']:7.1f} MB")
    else:
        print("\nHost QEMU: No data collected.") # Added else for clarity
    
    # QEMU Display FPS statistics
    if qemu_display_fps_samples:
        final_aggregated_stats['qemu_display_fps'] = {
            'average': sum(qemu_display_fps_samples) / len(qemu_display_fps_samples),
            'maximum': max(qemu_display_fps_samples),
            'minimum': min(qemu_display_fps_samples)
        }
        avg_display_count = sum(qemu_display_count_samples) / len(qemu_display_count_samples) if qemu_display_count_samples else 0
        print(f"\nQEMU Display FPS (Aggregated over {len(qemu_display_fps_samples)} samples, ~{avg_display_count:.0f} displays):")
        print(f"  Display FPS  - Avg: {final_aggregated_stats['qemu_display_fps']['average']:5.1f}, Max: {final_aggregated_stats['qemu_display_fps']['maximum']:5.1f}, Min: {final_aggregated_stats['qemu_display_fps']['minimum']:5.1f}")
    else:
        print("\nQEMU Display FPS: No data collected.")
    
    # GPU aggregated statistics
    print_final_gpu_stats = False
    if gpu_util_samples: 
        final_aggregated_stats['gpu_util_percent'] = {
            'average': sum(gpu_util_samples) / len(gpu_util_samples),
            'maximum': max(gpu_util_samples),
            'minimum': min(gpu_util_samples)
        }
        print(f"\nHost GPU Statistics:")
        print(f"  Overall GPU Util - Avg: {final_aggregated_stats['gpu_util_percent']['average']:5.1f}%, Max: {final_aggregated_stats['gpu_util_percent']['maximum']:5.1f}%, Min: {final_aggregated_stats['gpu_util_percent']['minimum']:5.1f}%")
        print_final_gpu_stats = True

    if gpu_mem_used_samples:
        final_aggregated_stats['gpu_mem_used_mb'] = {
            'average': sum(gpu_mem_used_samples) / len(gpu_mem_used_samples),
            'maximum': max(gpu_mem_used_samples),
            'minimum': min(gpu_mem_used_samples)
        }
        # If overall GPU util wasn't printed, print a header for total GPU mem.
        if not print_final_gpu_stats: 
             print(f"\nHost GPU Statistics:")
        print(f"  Total GPU Memory - Avg: {final_aggregated_stats['gpu_mem_used_mb']['average']:7.1f} MB, Max: {final_aggregated_stats['gpu_mem_used_mb']['maximum']:7.1f} MB, Min: {final_aggregated_stats['gpu_mem_used_mb']['minimum']:7.1f} MB")
        
        # Add total memory info if available
        if gpu_mem_total_samples:
            avg_total = sum(gpu_mem_total_samples) / len(gpu_mem_total_samples)
            avg_used_percent = (final_aggregated_stats['gpu_mem_used_mb']['average'] / avg_total) * 100
            print(f"  GPU Memory Usage   - {avg_used_percent:.1f}% of {avg_total:.0f} MB total")
        
        print_final_gpu_stats = True

    if qemu_gpu_mem_samples:
        final_aggregated_stats['qemu_gpu_mem_used_mb'] = {
            'average': sum(qemu_gpu_mem_samples) / len(qemu_gpu_mem_samples),
            'maximum': max(qemu_gpu_mem_samples),
            'minimum': min(qemu_gpu_mem_samples)
        }
        # If other GPU stats weren't printed, print a header for QEMU GPU mem.
        if not print_final_gpu_stats: 
             print(f"\nHost GPU Statistics:")
        print(f"  QEMU GPU Memory  - Avg: {final_aggregated_stats['qemu_gpu_mem_used_mb']['average']:7.1f} MB, Max: {final_aggregated_stats['qemu_gpu_mem_used_mb']['maximum']:7.1f} MB, Min: {final_aggregated_stats['qemu_gpu_mem_used_mb']['minimum']:7.1f} MB")
        print_final_gpu_stats = True

    if not print_final_gpu_stats:
        print("\nHost GPU: No data collected.")

    # Guest aggregated statistics
    if successful_guest_measurements > 0:
        avg_guest_cpu_by_pid = calculate_average(accumulated_guest_cpu_data, successful_guest_measurements)
        avg_guest_mem_by_pid = calculate_average(accumulated_guest_mem_data, successful_guest_measurements)
        
        final_aggregated_stats['guest_average_cpu_by_pid'] = avg_guest_cpu_by_pid
        final_aggregated_stats['guest_average_memory_by_pid'] = avg_guest_mem_by_pid
        final_aggregated_stats['guest_successful_measurements'] = successful_guest_measurements
        
        print(f"\nGuest (Android) - Top 5 Processes (Average over {successful_guest_measurements} successful measurements):")
        print("  CPU Usage:")
        for pid, usage in sorted(avg_guest_cpu_by_pid.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"    PID {pid}: {usage:5.2f}%")
        print("  Memory Usage:")
        for pid, usage in sorted(avg_guest_mem_by_pid.items(), key=lambda x: x[1], reverse=True)[:5]:
            print(f"    PID {pid}: {usage:7.2f} MB")
    else:
        print("\nGuest (Android): No measurements were successfully taken during the session.")
        final_aggregated_stats['guest_average_cpu_by_pid'] = None
        final_aggregated_stats['guest_average_memory_by_pid'] = None
        final_aggregated_stats['guest_successful_measurements'] = 0

    print()
    run_automated_fps_test(device)
    return final_aggregated_stats

def get_all_system_stats(device, duration=-1, interval=1):
    """
    Monitors QEMU CPU/memory, GPU usage, and Android guest usage simultaneously,
    prints real-time output, and returns aggregated statistics.
    """
    # Data collection lists for aggregation
    qemu_cpu_samples = []
    qemu_mem_samples = []
    gpu_util_samples = [] # Overall GPU utilization
    gpu_mem_used_samples = [] # Total GPU memory used
    gpu_mem_total_samples = [] # Total GPU memory available
    qemu_gpu_mem_samples = [] # QEMU-specific GPU memory
    qemu_display_fps_samples = [] # QEMU display FPS averages
    qemu_display_count_samples = [] # Number of displays
    
    # For guest, we aggregate averages at the end using calculate_average
    accumulated_guest_cpu_data = {}
    accumulated_guest_mem_data = {}
    
    sample_count = 0
    successful_guest_measurements = 0
    
    start_time = time.time()
    
    try:
        while True:
            # Check if we should stop (for finite duration)
            if duration != -1 and time.time() - start_time >= duration:
                break
            
            iteration_start_time = time.time()
            sample_count += 1
            
            print(f"Sample {sample_count} @ {time.strftime('%H:%M:%S')}: ", end="")
            
            current_snapshot = get_current_system_stats(device)

            # Process and print QEMU host stats (CPU/RAM)
            if current_snapshot['qemu_cpu_percent'] is not None:
                qemu_cpu_samples.append(current_snapshot['qemu_cpu_percent'])
                qemu_mem_samples.append(current_snapshot['qemu_mem_mb'])
                print(f"qemu_cpu {current_snapshot['qemu_cpu_percent']:.1f}%, qemu_mem {current_snapshot['qemu_mem_mb']:.1f} MB", end="")
            else:
                print("QEMU host data not available", end="")

            # Process and print GPU stats (Overall Util + Total GPU Memory + QEMU GPU Memory)
            print_gpu_stats = []
            if current_snapshot['gpu_util_percent'] is not None:
                gpu_util_samples.append(current_snapshot['gpu_util_percent'])
                print_gpu_stats.append(f"gpu_util {current_snapshot['gpu_util_percent']:.0f}%")
            
            if current_snapshot['gpu_mem_used_mb'] is not None:
                gpu_mem_used_samples.append(current_snapshot['gpu_mem_used_mb'])
                if current_snapshot['gpu_mem_total_mb'] is not None:
                    gpu_mem_total_samples.append(current_snapshot['gpu_mem_total_mb'])
                    gpu_mem_percent = (current_snapshot['gpu_mem_used_mb'] / current_snapshot['gpu_mem_total_mb']) * 100
                    print_gpu_stats.append(f"gpu_mem {current_snapshot['gpu_mem_used_mb']:.0f}/{current_snapshot['gpu_mem_total_mb']:.0f} MB ({gpu_mem_percent:.1f}%)")
                else:
                    print_gpu_stats.append(f"gpu_mem {current_snapshot['gpu_mem_used_mb']:.0f} MB")
            
            if current_snapshot['qemu_gpu_mem_used_mb'] is not None:
                qemu_gpu_mem_samples.append(current_snapshot['qemu_gpu_mem_used_mb'])
                if current_snapshot['gpu_mem_total_mb'] is not None:
                    qemu_gpu_mem_percent = (current_snapshot['qemu_gpu_mem_used_mb'] / current_snapshot['gpu_mem_total_mb']) * 100
                    print_gpu_stats.append(f"qemu_gpu_mem {current_snapshot['qemu_gpu_mem_used_mb']:.0f}/{current_snapshot['gpu_mem_total_mb']:.0f} MB ({qemu_gpu_mem_percent:.1f}%)")
                else:
                    print_gpu_stats.append(f"qemu_gpu_mem {current_snapshot['qemu_gpu_mem_used_mb']:.0f} MB")
            
            if print_gpu_stats:
                print(", " + ", ".join(print_gpu_stats), end="")
            else:
                print(", GPU data not available", end="")

            # Process and print QEMU Display FPS
            if current_snapshot['qemu_display_fps_avg'] is not None:
                qemu_display_fps_samples.append(current_snapshot['qemu_display_fps_avg'])
                qemu_display_count_samples.append(current_snapshot['qemu_display_fps_count'])
                print(f", display_fps {current_snapshot['qemu_display_fps_avg']:.1f} ({current_snapshot['qemu_display_fps_count']} displays)", end="")
            else:
                print(", QEMU display FPS not available", end="")

            # Process and print Guest stats
            if current_snapshot['guest_cpu_by_pid'] and current_snapshot['guest_mem_by_pid']:
                successful_guest_measurements += 1
                # Accumulate guest data for final average
                for pid, cpu_val in current_snapshot['guest_cpu_by_pid'].items():
                    accumulated_guest_cpu_data.setdefault(pid, []).append(cpu_val)
                for pid, mem_val in current_snapshot['guest_mem_by_pid'].items():
                    accumulated_guest_mem_data.setdefault(pid, []).append(mem_val)
                
                top_cpu_pids = sorted(current_snapshot['guest_cpu_by_pid'].items(), key=lambda x: x[1], reverse=True)[:3]
                if top_cpu_pids:
                    guest_cpu_str = ", ".join([f"{pid}:{cpu_val:.1f}%" for pid, cpu_val in top_cpu_pids])
                    print(f", guest Top CPU:")
                    print(f"    {guest_cpu_str}")
                else:
                    print(", guest no CPU data to display.")
            else:
                print(", guest data not available.")
            
            elapsed_this_iteration = time.time() - iteration_start_time
            sleep_time = max(0, interval - elapsed_this_iteration)
            time.sleep(sleep_time)
    
    except KeyboardInterrupt:
        # Still print final statistics
        pass
    finally:
        # Clean up cached telnet connection
        close_qemu_monitor_connection()
    
    # Print final statistics regardless of how we exited
    return print_final_statistics(device, sample_count, qemu_cpu_samples, qemu_mem_samples, gpu_util_samples,
                                 gpu_mem_used_samples, gpu_mem_total_samples, qemu_gpu_mem_samples,
                                 qemu_display_fps_samples, qemu_display_count_samples,
                                 accumulated_guest_cpu_data, accumulated_guest_mem_data, successful_guest_measurements)

if __name__ == "__main__":
    duration = -1  # Default duration in seconds (-1 means infinite)
    interval = 1   # Default interval in seconds

    if "--help" in sys.argv or "-h" in sys.argv:
        print("Usage: python3 cpu_profiler.py [duration] [interval]")
        print()
        print("Monitors Android guest, QEMU host process, and GPU usage simultaneously.")
        print()
        print("Arguments:")
        print("  duration      Monitoring duration in seconds (default: -1 for infinite)")
        print("  interval      Sampling interval in seconds (default: 1)")
        print()
        print("Example:")
        print("  python3 cpu_profiler.py 60 2   # Monitor for 60 seconds with a 2-second interval")
        print("  python3 cpu_profiler.py -1 1   # Monitor infinitely with a 1-second interval")
        print("  python3 cpu_profiler.py        # Monitor infinitely with default 1-second interval")
        sys.exit(0)

    if len(sys.argv) > 1:
        try:
            duration = int(sys.argv[1])
        except ValueError:
            print(f"Error: Invalid duration '{sys.argv[1]}'. Must be an integer.")
            sys.exit(1)
    
    if len(sys.argv) > 2:
        try:
            interval = int(sys.argv[2])
        except ValueError:
            print(f"Error: Invalid interval '{sys.argv[2]}'. Must be an integer.")
            sys.exit(1)

    if duration <= 0 and duration != -1:
        print("Error: Duration must be positive or -1 for infinite monitoring.")
        sys.exit(1)
    
    if interval <= 0:
        print("Error: Interval must be positive.")
        sys.exit(1)

    if duration == -1:
        print(f"Starting infinite monitoring with {interval}s interval. Press Ctrl+C to stop.")
    else:
        print(f"Starting monitoring for {duration}s with {interval}s interval.")
    print("=" * 80)
    
    # Ensure ADB is connected for guest monitoring
    detect_adb()
    device = get_adb_devices(return_first=True)

    get_all_system_stats(device, duration, interval)