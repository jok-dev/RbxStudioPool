"""Probe 5b: do multi-client test windows take focus when the server is launched in the background?

    python3 probe5b_focus_clients.py [--clients 2]

Launches a test server with `open -n -g -j` (marker place swapped in at server.rbxl and put back),
waits for its clients, then adds one player by running a copied client line directly and one through
`open -n -g -j`. Records every app activation (with --counter as in probe 5) and which phase it fell in.
"""

import argparse
import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_APP, MacStudioMcp, server_rbxl
from probe1_multi import studio_pids
from probe2_args import args_of, studio_children, wait_new
from probe5_focus import Watcher

STATE = Path(__file__).resolve().parent / "state"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=2)
    ap.add_argument("--counter", choices=["refocus", "hide", "none"], default="none")
    args = ap.parse_args()
    run = STATE / f"probe5b-{int(time.time())}"
    run.mkdir(parents=True)
    copy = run / "copy.rbxl"
    shutil.copy(STATE / "marker.rbxl", copy)
    w = Watcher()
    w.counter = args.counter
    ours = set()
    before = studio_pids()
    with server_rbxl(STATE / "marker.rbxl"):
        mcp = MacStudioMcp()
        try:
            known = {s["id"] for s in mcp.list_studios()}
            w.phase = "server+clients"
            r = subprocess.run(["open", "-n", "-g", "-j", "-a", str(STUDIO_APP), "--args", "-task", "StartServer",
                                "-localProjectFile", str(copy), "-placeId", "0", "-universeId", "0", "-port", "0",
                                "-creatorId", "0", "-creatorType", "0", "-placeVersion", "0",
                                "-numTestServerPlayersUponStartup", str(args.clients)])
            if r.returncode != 0:
                raise RuntimeError(f"open failed with {r.returncode}")
            t0 = time.time()
            while True:
                ours |= studio_pids() - before
                w.studio |= ours
                try:
                    ids, _ = wait_new(mcp, known, 1 + args.clients, timeout=1)
                    break
                except TimeoutError:
                    if time.time() - t0 > 150:
                        raise
            known |= set(ids)
            print(f"server + {args.clients} clients in the MCP after {time.time() - t0:.0f}s")
            time.sleep(5)
            server = next(p for p in ours if "-task StartServer" in args_of(p))
            line = args_of(studio_children(server)[0]).split()
            for how, inst in (("direct", "StudioPlayer_7"), ("open-bg", "StudioPlayer_8")):
                w.phase = f"add-{how}"
                parts = list(line)
                parts[parts.index("-instanceId") + 1] = inst
                if how == "direct":
                    subprocess.Popen(parts, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["open", "-n", "-g", "-j", "-a", str(STUDIO_APP), "--args", *parts[1:]])
                t1 = time.time()
                while True:
                    ours |= studio_pids() - before
                    w.studio |= ours
                    try:
                        new, _ = wait_new(mcp, known, 1, timeout=1)
                        break
                    except TimeoutError:
                        if time.time() - t1 > 90:
                            raise
                known |= set(new)
                print(f"added {how} in the MCP after {time.time() - t1:.0f}s")
                time.sleep(5)
            w.phase = "idle"
            time.sleep(3)
        finally:
            mcp.close()
            ours |= studio_pids() - before
            for pid in ours:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"sent SIGKILL to {sorted(ours)}")
            time.sleep(0.5)
            w.close()
    print(f"\nfront app before the probe: {w.home[1]} (pid {w.home[0]})")
    t0 = w.events[0][0] if w.events else 0
    for t, pid, name, phase in w.events:
        print(f"  +{t - t0:6.2f}s {phase:15s} {name} ({pid}) {'STUDIO' if pid in w.studio else ''}")
    print(f"{len(w.events)} activations in all")


if __name__ == "__main__":
    main()
