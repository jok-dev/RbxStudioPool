"""Probe 9: does a leftover AutoRecovery file bring up a dialog on the next launch?

    python3 probe9_autorecovery.py

Needs Studio's Auto-Recovery Interval set to 1 minute (`~/Library/Roblox/GlobalSettings_13.xml`).
Builds `windows` first: `swiftc -O windows.swift -o state/windows`.
1. Launches the bundle binary on a copy named slot1.rbxl, waits for its AutoRecovery file, SIGKILLs it.
2. Relaunches the same path plainly, then with `open -n -g -j`. For each: the Studio's windows,
   whether `execute_luau` answers, and what its log says about recovery.
3. Deletes the AutoRecovery files and relaunches once more.
Every Studio the probe starts is SIGKILLed; AutoRecovery files of slot1 are deleted at the end.
"""

import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_APP, STUDIO_BIN, MacStudioMcp
from probe1_multi import studio_pids
from probe7_stop import alive

STATE = Path(__file__).resolve().parent / "state"
AUTOSAVES = Path.home() / "Library" / "Application Support" / "Roblox" / "RobloxStudio" / "AutoSaves"
LOGS = Path.home() / "Library" / "Logs" / "Roblox"


def recovery_files():
    return sorted(AUTOSAVES.glob("slot1_AutoRecovery_*.rbxl"))


def launch(place, hidden):
    before = studio_pids()
    if hidden:
        subprocess.run(["open", "-n", "-g", "-j", "-a", str(STUDIO_APP), "--args", str(place)], check=True)
    else:
        subprocess.Popen([str(STUDIO_BIN), str(place)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 20:
        new = studio_pids() - before
        if new:
            return new.pop()
        time.sleep(0.5)
    raise RuntimeError("no new Studio process appeared")


def log_of(pid):
    for f in sorted(LOGS.glob("*_Studio_*_last.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]:
        text = f.read_text(errors="replace")
        if f"process '{pid}'" in text:
            return text
    return ""


def windows(pid):
    return subprocess.run([str(STATE / "windows"), str(pid)], capture_output=True, text=True).stdout.strip()


def check(mcp, known, pid, label):
    """Wait for the new Studio in the MCP, try Luau, list windows, grep the log for recovery."""
    t0 = time.time()
    sid = None
    while time.time() - t0 < 60 and sid is None:
        new = [s["id"] for s in mcp.list_studios() if s["id"] not in known]
        sid = new[0] if new else None
        time.sleep(1)
    print(f"[{label}] pid {pid}: in the MCP after {time.time() - t0:.0f}s" if sid else f"[{label}] pid {pid}: not in the MCP after 60s")
    if sid:
        t1 = time.time()
        try:
            out = mcp.luau(sid, "return game:GetService('Workspace').Name", timeout=30).strip()
            print(f"[{label}] execute_luau answered in {time.time() - t1:.1f}s: {out[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] execute_luau failed after {time.time() - t1:.1f}s: {e}")
    time.sleep(5)
    print(f"[{label}] windows:\n{windows(pid) or '(none)'}")
    lines = [l for l in log_of(pid).splitlines() if "recover" in l.lower()]
    print(f"[{label}] log lines about recovery: {len(lines)}")
    for l in lines[:8]:
        print("   ", l[:220])
    return sid


def kill(pid):
    subprocess.run(["kill", "-9", str(pid)], capture_output=True)
    while alive(pid):
        time.sleep(0.2)


def main():
    run = STATE / f"probe9-{int(time.time())}"
    run.mkdir(parents=True)
    place = run / "slot1.rbxl"
    shutil.copy(STATE / "blank.rbxl", place)
    for f in recovery_files():
        f.unlink()
    mcp = MacStudioMcp()
    pids = []
    try:
        # 1. Leave an AutoRecovery file behind.
        known = {s["id"] for s in mcp.list_studios()}
        pid = launch(place, hidden=False)
        pids.append(pid)
        sid = check(mcp, known, pid, "first launch")
        if sid:  # an AutoRecovery file is only written for a place with unsaved changes
            mcp.luau(sid, "local p = Instance.new('Part') p.Name = 'Dirty' p.Parent = workspace return 'ok'")
        t0 = time.time()
        while not recovery_files() and time.time() - t0 < 180:
            time.sleep(2)
        print(f"AutoRecovery files after {time.time() - t0:.0f}s: {[f.name for f in recovery_files()]}")
        kill(pid)
        if not recovery_files():
            raise SystemExit("no AutoRecovery file was written; nothing to test")

        # 2. Relaunch the same path, plainly and hidden.
        for hidden in (False, True):
            label = "relaunch hidden" if hidden else "relaunch plain"
            known = {s["id"] for s in mcp.list_studios()}
            pid = launch(place, hidden)
            pids.append(pid)
            check(mcp, known, pid, label)
            kill(pid)
            print(f"[{label}] AutoRecovery files after: {[f.name for f in recovery_files()]}")

        # 3. Delete them first.
        for f in recovery_files():
            f.unlink()
        known = {s["id"] for s in mcp.list_studios()}
        pid = launch(place, hidden=False)
        pids.append(pid)
        check(mcp, known, pid, "relaunch after delete")
        kill(pid)
    finally:
        mcp.close()
        for pid in pids:
            kill(pid)
        for f in recovery_files():
            f.unlink()
        print(f"left no Studios: {not any(alive(p) for p in pids)}; deleted slot1 AutoRecovery files")


if __name__ == "__main__":
    main()
