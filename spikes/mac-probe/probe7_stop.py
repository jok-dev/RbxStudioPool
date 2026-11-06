"""Probe 7: stopping a multi-client test on a Mac.

    python3 probe7_stop.py

Starts a background test server with 2 clients (marker place swapped in at server.rbxl and put back).
1. SIGKILLs Player2's process and times how long until the server's player list drops it.
2. Sends the server SIGTERM and checks whether it exits; then SIGKILLs only the server and checks
   whether its remaining client exits too, and how fast.
Anything of the probe's still alive at the end is SIGKILLed.
"""

import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_APP, MacStudioMcp, server_rbxl
from probe1_multi import studio_pids
from probe2_args import args_of, kind_of, wait_new

STATE = Path(__file__).resolve().parent / "state"
PLAYERS = 'local t = {} for _, p in game.Players:GetPlayers() do table.insert(t, p.Name) end return table.concat(t, ",")'


def alive(pid):
    return subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0


def wait_dead(pids, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not any(alive(p) for p in pids):
            return time.time() - t0
        time.sleep(0.2)
    return None


def main():
    run = STATE / f"probe7-{int(time.time())}"
    run.mkdir(parents=True)
    copy = run / "copy.rbxl"
    shutil.copy(STATE / "marker.rbxl", copy)
    before = studio_pids()
    ours = set()
    with server_rbxl(STATE / "marker.rbxl"):
        mcp = MacStudioMcp()
        try:
            known = {s["id"] for s in mcp.list_studios()}
            subprocess.run(["open", "-n", "-g", "-j", "-a", str(STUDIO_APP), "--args", "-task", "StartServer",
                            "-localProjectFile", str(copy), "-placeId", "0", "-universeId", "0", "-port", "0",
                            "-creatorId", "0", "-creatorType", "0", "-placeVersion", "0",
                            "-numTestServerPlayersUponStartup", "2"], check=True)
            ids, t = wait_new(mcp, known, 3, timeout=150)
            ours = studio_pids() - before
            server = next(p for p in ours if "-task StartServer" in args_of(p))
            clients = {p: args_of(p).split("-instanceId ")[1].split()[0] for p in ours if p != server}
            server_sid = next(s for s in ids if kind_of(mcp, s) == "Server")
            print(f"server {server}, clients {clients}; players: {mcp.luau(server_sid, PLAYERS, datamodel='Server')}")

            # 1. Kill one client like a crash.
            victim = max(clients)
            subprocess.run(["kill", "-9", str(victim)])
            t0 = time.time()
            start = mcp.luau(server_sid, PLAYERS, datamodel="Server").strip()
            while True:
                now = mcp.luau(server_sid, PLAYERS, datamodel="Server").strip()
                if now.count(",") < start.count(",") or time.time() - t0 > 120:
                    break
                time.sleep(1)
            print(f"killed client {victim} ({clients[victim]}): server list went {start!r} -> {now!r} after {time.time() - t0:.0f}s")

            # 2. SIGTERM, then SIGKILL, the server alone.
            rest = [p for p in clients if p != victim]
            subprocess.run(["kill", str(server)])
            dt = wait_dead([server], 10)
            print(f"SIGTERM on server: {'exited after %.1fs' % dt if dt is not None else 'still alive after 10s'}")
            if dt is None:
                subprocess.run(["kill", "-9", str(server)])
                print(f"SIGKILL on server: exited after {wait_dead([server], 10)}s")
            dt = wait_dead(rest, 90)
            print(f"remaining client(s) {rest}: {'exited %.1fs after the server' % dt if dt is not None else 'still alive 90s after the server died'}")
            if dt is None:
                print(f"  still listed in the MCP: {[s['id'] for s in mcp.list_studios() if s['id'] in ids]}")
        finally:
            mcp.close()
            left = [p for p in ours if alive(p)]
            for pid in left:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"SIGKILLed leftovers: {left}")


if __name__ == "__main__":
    main()
