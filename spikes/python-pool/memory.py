"""How much memory a Studio holds, and when it has grown enough from use to be worth replacing. Needs no pool.

Each playtest leaves memory behind that Studio never gives back (FINDINGS.md, Spike 5), so a Studio that lives long
grows. Windows only, like the rest of the pool.
"""

import ctypes
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MB = 2 ** 20


class _MemoryCounters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
            "PrivateUsage")]


def private_mb(pid):
    """The process's private bytes in MB, or None if it is gone or can't be read. Never raises.
    Private bytes, not working set: Windows trims the working set of an idle process, so it falls on its own."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE  # the default c_int would truncate a 64-bit handle
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MemoryCounters), wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            counters = _MemoryCounters(cb=ctypes.sizeof(_MemoryCounters))
            if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return counters.PrivateUsage / MB
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def worn_reason(plays, growth_mb, max_plays, max_growth_mb):
    """Why a Studio counts as worn, or None. plays: playtests started on it. growth_mb: memory gained since its
    baseline (None if unknown, which only skips that check). A threshold of 0 is off."""
    if max_plays and plays >= max_plays:
        return f"{plays} playtests"
    if max_growth_mb and growth_mb is not None and growth_mb >= max_growth_mb:
        return f"grew {growth_mb / 1024:.1f} GB in use"
    return None
