"""Probe 3: does copying a place to ~/Documents/Roblox/server.rbxl make a test server run it?

    python3 probe3_server_rbxl.py

Moves the user's server.rbxl aside, copies state/marker.rbxl (Workspace attribute ProbeMark) in its
place, starts a test server on a blank copy and reads the attribute from the server. The user's file
is put back in all cases. Only the probe's own processes are killed.
"""

import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_BIN, MacStudioMcp
from probe2_args import tree, wait_new

STATE = Path(__file__).resolve().parent / "state"
SERVER_RBXL = Path.home() / "Documents" / "Roblox" / "server.rbxl"
BACKUP = SERVER_RBXL.with_name("server.rbxl.bak")
MARK = 'return tostring(workspace:GetAttribute("ProbeMark"))'


def main():
    if BACKUP.exists():
        raise SystemExit(f"{BACKUP} already exists; not overwriting it")
    run = STATE / f"probe3-{int(time.time())}"
    run.mkdir(parents=True)
    copy = run / "copy.rbxl"
    shutil.copy(STATE / "blank.rbxl", copy)
    had_file = SERVER_RBXL.exists()
    if had_file:
        SERVER_RBXL.rename(BACKUP)
    mcp = server = None
    try:
        shutil.copy(STATE / "marker.rbxl", SERVER_RBXL)
        written = SERVER_RBXL.stat().st_mtime
        mcp = MacStudioMcp()
        known = {s["id"] for s in mcp.list_studios()}
        server = subprocess.Popen(
            [str(STUDIO_BIN), "-task", "StartServer", "-localProjectFile", str(copy), "-placeId", "0",
             "-universeId", "0", "-port", "0", "-creatorId", "0", "-creatorType", "0", "-placeVersion", "0",
             "-numTestServerPlayersUponStartup", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        if server.poll() is not None:
            raise RuntimeError(f"server exited at once with code {server.returncode}")
        new, t = wait_new(mcp, known, 2, timeout=150)
        print(f"server + client in the MCP after {t:.0f}s")
        for sid in new:
            for dm in ("Server", "Client"):
                try:
                    print(f"  {sid} on {dm}: ProbeMark = {mcp.luau(sid, MARK, datamodel=dm, timeout=20).strip()}")
                    break
                except Exception:
                    pass
        changed = SERVER_RBXL.stat().st_mtime != written
        print(f"server.rbxl changed by the test: {changed}")
    finally:
        if mcp is not None:
            mcp.close()
        if server is not None:
            pids = tree(server.pid)
            for pid in pids:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"sent SIGKILL to {pids}")
            time.sleep(1)
        SERVER_RBXL.unlink(missing_ok=True)
        if had_file:
            BACKUP.rename(SERVER_RBXL)
        print(f"restored the user's server.rbxl: {SERVER_RBXL.exists() == had_file}")


if __name__ == "__main__":
    main()
