"""Probe 5: how often does Studio take focus, and what stops it?

    python3 probe5_focus.py --launch bin|open-bg [--counter refocus|hide|none] [--plays 2]

Watches app activations (state/focuswatch, built from focuswatch.swift) while one Studio launches on
a copy of state/blank.rbxl, loads, and starts and stops Play through the Studio MCP. Each time the
probe's Studio is activated, the counter gives focus back so the next grab shows up as its own event:
`refocus` re-activates the app that was in front before, `hide` hides Studio. `none` only records.
Prints each Studio activation per phase and how long Studio held focus each time.

Launch modes: `bin` runs the bundle binary; `open-bg` uses `open -n -g -j` (background, hidden).
"""

import argparse
import shutil
import subprocess
import threading
import time
from pathlib import Path

from macmcp import STUDIO_APP, STUDIO_BIN, MacStudioMcp
from probe1_multi import studio_pids

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
FOCUSWATCH = STATE / "focuswatch"


class Watcher:
    def __init__(self):
        self.proc = subprocess.Popen([str(FOCUSWATCH), "watch"], stdout=subprocess.PIPE, text=True, bufsize=1)
        self.events = []  # (time, pid, name)
        self.studio = set()  # pids that count as the probe's Studio
        self.counter = "none"
        self.home = None  # (pid, name) of the app in front before the probe
        self.phase = "start"
        self.ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        self.ready.wait(5)

    def _read(self):
        for line in self.proc.stdout:
            t, pid, name = line.rstrip("\n").split(" ", 2)
            t, pid = float(t), int(pid)
            if self.home is None:
                self.home = (pid, name)
                self.ready.set()
                continue
            self.events.append((t, pid, name, self.phase))
            if pid in self.studio and self.counter != "none":
                if self.counter == "refocus":
                    subprocess.run([str(FOCUSWATCH), "activate", str(self.home[0])])
                else:
                    subprocess.run([str(FOCUSWATCH), "hide", str(pid)])

    def close(self):
        self.proc.terminate()


def report(w):
    print(f"\nfront app before the probe: {w.home[1]} (pid {w.home[0]})")
    by_phase = {}
    for i, (t, pid, name, phase) in enumerate(w.events):
        if pid not in w.studio:
            continue
        nxt = next((e for e in w.events[i + 1:] if e[1] not in w.studio), None)
        held = f"{nxt[0] - t:.2f}s" if nxt else "kept it"
        by_phase.setdefault(phase, []).append(held)
    for phase in ("launch", "load", "play-start", "play-stop", "idle"):
        grabs = by_phase.get(phase, [])
        print(f"  {phase:10s}: {len(grabs)} Studio activations; held for {', '.join(grabs) or '-'}")
    print("\nall activations:")
    t0 = w.events[0][0] if w.events else 0
    for t, pid, name, phase in w.events:
        mark = "STUDIO" if pid in w.studio else ""
        print(f"  +{t - t0:6.2f}s {phase:10s} {name} ({pid}) {mark}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", choices=["bin", "open-bg"], default="bin")
    ap.add_argument("--counter", choices=["refocus", "hide", "none"], default="refocus")
    ap.add_argument("--plays", type=int, default=2)
    ap.add_argument("--shot", action="store_true", help="screen_capture during each Play")
    args = ap.parse_args()

    run = STATE / f"probe5-{int(time.time())}"
    run.mkdir(parents=True)
    place = run / "copy.rbxl"
    shutil.copy(STATE / "blank.rbxl", place)
    w = Watcher()
    w.counter = args.counter
    mcp = MacStudioMcp()
    ours = set()
    try:
        known = {s["id"] for s in mcp.list_studios()}
        before = studio_pids()
        w.phase = "launch"
        if args.launch == "bin":
            proc = subprocess.Popen([str(STUDIO_BIN), str(place)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ours.add(proc.pid)
            w.studio.add(proc.pid)
        else:
            r = subprocess.run(["open", "-n", "-g", "-j", "-a", str(STUDIO_APP), "--args", str(place)])
            if r.returncode != 0:
                raise RuntimeError(f"open failed with {r.returncode}")
        t0 = time.time()
        while not (studio_pids() - before):
            if time.time() - t0 > 10:
                raise RuntimeError("no Studio process appeared")
            time.sleep(0.05)
        ours |= studio_pids() - before
        w.studio |= ours
        sid = None
        while sid is None:
            new = [s["id"] for s in mcp.list_studios() if s["id"] not in known]
            sid = new[0] if new else None
            if time.time() - t0 > 120:
                raise TimeoutError("Studio never showed up in the MCP")
            time.sleep(0.5)
        w.phase = "load"
        while True:
            try:
                mcp.luau(sid, "return 1", timeout=20)
                break
            except Exception:
                time.sleep(1)
        print(f"Studio pid {sorted(ours)} ready after {time.time() - t0:.1f}s")
        time.sleep(5)
        for _ in range(args.plays):
            w.phase = "play-start"
            mcp.call("start_stop_play", {"is_start": True, "studio_id": sid})
            time.sleep(8)
            _, state = mcp.call("get_studio_state", {"studio_id": sid})
            print("during play:", " / ".join(l.strip("- ") for l in state.strip().splitlines()))
            if args.shot:
                result, text = mcp.call("screen_capture", {"capture_id": f"probe5_{int(time.time())}", "studio_id": sid})
                for c in result.get("content", []):
                    if c.get("type") == "image":
                        import base64
                        out = run / f"shot-{len(list(run.glob('shot-*')))}.png"
                        out.write_bytes(base64.b64decode(c["data"]))
                        print(f"screenshot saved: {out}")
                    else:
                        print(f"screen_capture {c.get('type')}: {str(c.get('text'))[:200]}")
            w.phase = "play-stop"
            mcp.call("start_stop_play", {"is_start": False, "studio_id": sid})
            time.sleep(5)
        w.phase = "idle"
        time.sleep(3)
    finally:
        mcp.close()
        for pid in ours:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        time.sleep(0.5)
        w.close()
        print(f"sent SIGKILL to {sorted(ours)}")
        report(w)


if __name__ == "__main__":
    main()
