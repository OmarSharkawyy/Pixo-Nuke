"""Read CPU, RAM, GPU, VRAM and temperature with the standard library.

psutil is used when it is installed. Otherwise Windows is read through ctypes,
Linux through /proc, and NVIDIA GPUs through nvidia-smi.
"""
from collections import namedtuple
import ctypes
import os
import shutil
import subprocess
import time

try:
    import psutil
except ImportError:
    psutil = None

# percent is 0-100 (degrees Celsius for "temp"); detail is shown in the tooltip.
Metric = namedtuple("Metric", "percent detail")

IS_WINDOWS = os.name == "nt"
GIB = 1024 ** 3
NVIDIA_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu"
NVIDIA_FAILURES = 3   # consecutive failures before nvidia-smi is left alone for a while
NVIDIA_PAUSE = 30.0   # seconds


def gib(value):
    return f"{value / GIB:.1f} GB"


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                ("total_physical", ctypes.c_uint64), ("available_physical", ctypes.c_uint64),
                ("total_page_file", ctypes.c_uint64), ("available_page_file", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64), ("available_virtual", ctypes.c_uint64),
                ("available_extended", ctypes.c_uint64)]


class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


def windows_cpu_times():
    idle, kernel, user = FileTime(), FileTime(), FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    ticks = lambda value: (value.high << 32) | value.low
    return ticks(idle), ticks(kernel) + ticks(user)  # kernel time already includes idle time


def parse_proc_stat(text):
    """(idle, total) from the first line of /proc/stat."""
    try:
        fields = [int(item) for item in text.splitlines()[0].split()[1:]]
        return fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields[:8])
    except (IndexError, ValueError):
        return None


def cpu_times():
    """Cumulative (idle, total) CPU time, or None when this system cannot report it."""
    if IS_WINDOWS:
        return windows_cpu_times()
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            return parse_proc_stat(handle.read())
    except OSError:
        return None


class CpuMeter:
    """CPU load across all cores since the previous call."""

    def __init__(self):
        self.previous = None

    def read(self):
        if psutil is not None:
            return psutil.cpu_percent(interval=None)
        times, previous = cpu_times(), self.previous
        self.previous = times
        if times is None or previous is None:
            return None
        idle, total = times[0] - previous[0], times[1] - previous[1]
        return None if total <= 0 else max(0.0, min(100.0, 100.0 * (1 - idle / total)))


def parse_meminfo(text):
    """(used_bytes, total_bytes) from /proc/meminfo text."""
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * 1024
    total, available = values.get("MemTotal"), values.get("MemAvailable", values.get("MemFree"))
    return (total - available, total) if total and available is not None else None


def memory():
    """(used_bytes, total_bytes) of system RAM, or None."""
    if psutil is not None:
        info = psutil.virtual_memory()
        return info.total - info.available, info.total
    if IS_WINDOWS:
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.total_physical - status.available_physical, status.total_physical
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            return parse_meminfo(handle.read())
    except OSError:
        return None


def find_nvidia_smi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for candidate in (r"C:\Windows\System32\nvidia-smi.exe",
                      r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        if os.path.isfile(candidate):
            return candidate
    return None


def parse_nvidia_smi(text, index=0):
    """Metrics for one GPU from `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    cells = [cell.strip() for cell in lines[min(max(index, 0), len(lines) - 1)].split(",")]

    def number(position):
        try:
            return float(cells[position])
        except (IndexError, ValueError):  # "[N/A]" and similar
            return None

    load, used, total, degrees = number(0), number(1), number(2), number(3)
    found = {}
    if load is not None:
        found["gpu"] = Metric(load, f"GPU load {load:.0f}%")
    if used is not None and total:
        found["vram"] = Metric(100.0 * used / total, f"{used / 1024:.1f} / {total / 1024:.1f} GB")
    if degrees is not None:
        found["temp"] = Metric(degrees, f"{degrees:.0f} °C")
    return found


def read_nvidia(path, index=0, timeout=3):
    """Ask nvidia-smi for one reading; None if it fails or takes too long."""
    try:
        result = subprocess.run([path, f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, errors="replace", timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_nvidia_smi(result.stdout, index) if result.returncode == 0 else None


def server_metrics(stats, device=0):
    """RAM and VRAM as ComfyUI reports them in a /system_stats reply."""
    found = {}
    system = (stats or {}).get("system") or {}
    total, free = system.get("ram_total"), system.get("ram_free")
    if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total > 0:
        used = total - free
        found["ram"] = Metric(100.0 * used / total, f"{gib(used)} / {gib(total)} (ComfyUI)")
    devices = (stats or {}).get("devices")
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        card = devices[min(max(device, 0), len(devices) - 1)]
        total, free = card.get("vram_total"), card.get("vram_free")
        if isinstance(total, (int, float)) and isinstance(free, (int, float)) and total > 0:
            used = total - free
            found["vram"] = Metric(100.0 * used / total, f"{gib(used)} / {gib(total)} (ComfyUI)")
    return found


class LocalSampler:
    """Readings for the computer Nuke runs on."""

    def __init__(self, gpu_index=0, now=time.monotonic):
        self.gpu_index = gpu_index
        self.now = now
        self.cpu = CpuMeter()
        self.nvidia = find_nvidia_smi()
        self.failures = 0
        self.paused_until = 0.0

    def sample(self):
        found = {}
        load = self.cpu.read()
        if load is not None:
            found["cpu"] = Metric(load, f"{load:.0f}% of all cores")
        ram = memory()
        if ram:
            used, total = ram
            found["ram"] = Metric(100.0 * used / total, f"{gib(used)} / {gib(total)}")
        found.update(self._gpu())
        return found

    def _gpu(self):
        if self.nvidia is None or self.now() < self.paused_until:
            return {}
        reading = read_nvidia(self.nvidia, self.gpu_index)
        if reading is None:
            self.failures += 1
            if self.failures >= NVIDIA_FAILURES:  # Do not spawn a failing process every second.
                self.failures, self.paused_until = 0, self.now() + NVIDIA_PAUSE
            return {}
        self.failures = 0
        return reading
