"""Probe 1: can several Studios run at once on a Mac, each with its own place?

    python3 probe1_multi.py [N] [--mode bin|open] [--keep]

Launches N Studios one at a time, each on its own copy of state/blank.rbxl, and finds each one's
Studio MCP id by diffing list_roblox_studios before and after its launch. Each Studio is asked for
a marker written into its copy, so a Studio that opened the wrong file shows up. Only Studios this
probe started are killed at the end (unless --keep).
"""

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

from macmcp import STUDIO_APP, STUDIO_BIN, MacStudioMcp

STATE = Path(__file__).resolve().parent / "state"
TEMPLATE = STATE / "blank.rbxl"
MARK = 'return tostring(workspace:GetAttribute("ProbeMark")) .. " | " .. game.Name'


def studio_pids():
    out = subprocess.run(["pgrep", "-f", str(STUDIO_BIN)], capture_output=True, text=True).stdout
    return {int(p) for p in out.split()}


def launch(mode, place):
    before = studio_pids()
    if mode == "bin":
        proc = subprocess.Popen([str(STUDIO_BIN), str(place)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        if proc.poll() is not None:
            raise RuntimeError(f"Studio exited at once with code {proc.returncode}")
    else:
        r = subprocess.run(["open", "-n", "-a", str(STUDIO_APP), "--args", str(place)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"open failed ({r.returncode}): {r.stderr.strip()}")
        time.sleep(3)
    new = studio_pids() - before
    if not new:
        raise RuntimeError("no new Studio process appeared")
    return new


def wait_new_studio(mcp, known, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = [s for s in mcp.list_studios() if s["id"] not in known]
        if new:
            return new, time.time() - t0
        time.sleep(1)
    raise TimeoutError(f"no new Studio in the MCP after {timeout}s")


def wait_luau(mcp, sid, timeout=90):
    t0 = time.time()
    while True:
        try:
            return mcp.luau(sid, MARK, timeout=20), time.time() - t0
        except Exception as e:
            if time.time() - t0 > timeout:
                raise TimeoutError(f"{sid}: Luau never answered: {e}")
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, nargs="?", default=3)
    ap.add_argument("--mode", choices=["bin", "open"], default="bin")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    run = STATE / f"probe1-{int(time.time())}"
    run.mkdir(parents=True)
    mcp = MacStudioMcp()
    ours = set()
    results = []
    try:
        known = {s["id"] for s in mcp.list_studios()}
        print(f"Studios already in the MCP: {len(known)}")
        for i in range(1, args.n + 1):
            place = run / f"slot{i}.rbxl"
            shutil.copy(TEMPLATE, place)
            t0 = time.time()
            pids = launch(args.mode, place)
            ours |= pids
            print(f"[{i}] launched pids {sorted(pids)}")
            new, t_mcp = wait_new_studio(mcp, known)
            known |= {s["id"] for s in new}
            if len(new) != 1:
                print(f"[{i}] WARNING: {len(new)} new Studios appeared: {new}")
            sid = new[0]["id"]
            # Write a marker into this Studio's DataModel, then read it back.
            mark = f"slot{i}"
            _, t_luau = wait_luau(mcp, sid)
            mcp.luau(sid, f'workspace:SetAttribute("ProbeMark", "{mark}") return "ok"')
            back = mcp.luau(sid, MARK)
            print(f"[{i}] studio {sid} in MCP after {t_mcp:.0f}s, Luau after +{t_luau:.0f}s, reads back: {back.strip()}")
            results.append({"slot": i, "pids": sorted(pids), "studio": new[0], "t_mcp": round(t_mcp, 1),
                            "t_total": round(time.time() - t0, 1), "readback": back.strip()})
        # Re-read every marker at the end: each Studio must still hold its own.
        print("\nfinal check:")
        for r in results:
            print(f"  slot{r['slot']}: {mcp.luau(r['studio']['id'], MARK).strip()}")
        print(f"\nStudio processes now: {sorted(studio_pids())}, ours: {sorted(ours)}")
        (run / "results.json").write_text(json.dumps(results, indent=2))
    finally:
        mcp.close()
        if not args.keep:
            # Studio ignores SIGTERM (README), so go straight to SIGKILL.
            for pid in ours:
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
            print(f"sent SIGKILL to {sorted(ours)}")


if __name__ == "__main__":
    main()
