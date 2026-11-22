# Running Studio on Linux

How Roblox Studio was set up on a headless Linux server so it could be logged in, driven through the Studio MCP, and run several copies at once. This is the reference for the pool's Linux backend. The findings behind it, including memory numbers, are Spikes 5 and 6 in [FINDINGS.md](../FINDINGS.md).

Tested on Debian 13 (kernel 6.12) with a Ryzen 9700X and its integrated Radeon GPU, Vinegar 1.9.4 (Flatpak `org.vinegarhq.Vinegar`, Wine "kombucha", DXVK 2.7.1), Studio version-8d7a1009c17e4ff8, in November 2026. Paths below use the user `debian`.

## Pieces

| Piece | What it does |
|---|---|
| sway (`WLR_BACKENDS=headless`) | A Wayland desktop with no monitor, rendering on the GPU. Studio's window lives here. |
| Xwayland | Wine draws through X11. Turned on in the sway config. |
| Vinegar (Flatpak) | Downloads Studio, runs it under its own Wine build with DXVK. |
| A browser shim | Lets Studio open the host's Firefox, so login works. |
| `StudioMCP.exe` under Wine | The Studio MCP server, from Studio's own version folder. |
| wayvnc + noVNC | Lets a person see and click the desktop, for login and debugging. |
| Kernel samepage merging | Merges identical memory pages across Studio copies. |

## Packages

```sh
sudo apt install sway xwayland wayvnc novnc websockify grim wlrctl wtype wl-clipboard firefox-esr flatpak
flatpak install --user flathub org.vinegarhq.Vinegar
```

The GPU needs Mesa's Vulkan driver (`mesa-vulkan-drivers` on AMD/Intel). Check `/dev/dri/renderD128` exists and the user can open it (the `render` group).

Don't install `xdg-desktop-portal` and its backends for this. On a headless host it made the login flow time out and left a blank window. The browser shim below replaces it.

## The headless desktop

`sway.conf`:

```
output HEADLESS-1 resolution 1920x1080 position 0 0
xwayland enable
default_border none
exec wayvnc 127.0.0.1 5900
```

Start it:

```sh
export XDG_RUNTIME_DIR=/tmp/studio-headless-run
mkdir -p "$XDG_RUNTIME_DIR" && chmod 700 "$XDG_RUNTIME_DIR"
export WLR_BACKENDS=headless
export WLR_LIBINPUT_NO_DEVICES=1
export WLR_RENDER_DRM_DEVICE=/dev/dri/renderD128
exec dbus-run-session sway -c sway.conf
```

Anything that talks to this desktop afterwards (Vinegar, `grim`, `wlrctl`, `wtype`, `wl-copy`) needs `XDG_RUNTIME_DIR=/tmp/studio-headless-run` and `WAYLAND_DISPLAY=wayland-1`. Start Vinegar from inside sway (an `exec` line in the config, or `swaymsg exec`) so it inherits the session's environment.

The virtual output runs at 60 Hz, which caps Studio at 60 fps.

## Viewing and input

wayvnc listens on `127.0.0.1:5900`. To view it from a browser, run noVNC next to it and tunnel the port over SSH:

```sh
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900   # on the server
ssh -N -L 6080:127.0.0.1:6080 <server>                            # on your machine
# then open http://localhost:6080/vnc.html
```

Scripted input, for when the Studio MCP can't do something:

- Screenshot: `grim -s 0.6 shot.png`.
- Mouse: `wlrctl pointer move <dx> <dy>`, `wlrctl pointer click left`.
- Text: `wtype 'text'`. Ctrl combinations through `wtype` are unreliable in Wine. For text, `wl-copy` plus a paste is more reliable. Studio's command bar autocompletes as you type and mangles typed code: use the Studio MCP's `execute_luau` instead.

## Logging in: the browser shim

Studio's login opens a browser. Under Wine that goes through `winebrowser`, which calls `xdg-open`, and inside the Flatpak there's none that works. The fix is an `xdg-open` that opens Firefox on the host.

`~/.var/app/org.vinegarhq.Vinegar/data/open-host-browser.sh`:

```sh
#!/bin/sh
exec flatpak-spawn --host \
	--env=WAYLAND_DISPLAY=wayland-1 \
	--env=XDG_RUNTIME_DIR=/tmp/studio-headless-run \
	--env=DISPLAY=:0 \
	--env=MOZ_ENABLE_WAYLAND=1 \
	--env=XDG_DATA_DIRS=/home/debian/.local/share/flatpak/exports/share:/usr/local/share:/usr/share \
	firefox-esr "$@"
```

Put it first on Vinegar's `PATH` as `xdg-open`, and let the app call `flatpak-spawn --host`:

```sh
V=/home/debian/.var/app/org.vinegarhq.Vinegar/data
chmod +x $V/open-host-browser.sh
mkdir -p $V/bin && ln -sf $V/open-host-browser.sh $V/bin/xdg-open
flatpak override --user --talk-name=org.freedesktop.Flatpak org.vinegarhq.Vinegar
flatpak override --user --env=PATH=$V/bin:/app/bin:/usr/bin:/usr/lib/extensions/vulkan/gamescope/bin org.vinegarhq.Vinegar
```

The `WineBrowser` registry key was also pointed at the script (`wine reg add "HKCU\Software\Wine\WineBrowser" /v Browsers /d <script>`). The shim on `PATH` is what was confirmed to work. Whether the key is needed as well wasn't tested.

Then log in once over VNC. Firefox opens on the headless desktop, and the login stays in Studio's Wine prefix for later launches. To get a password in without typing it over VNC, `wl-copy` it on the server and paste.

## Opening places

```sh
flatpak run org.vinegarhq.Vinegar run /path/to/copy.rbxl
```

**Vinegar is single-instance.** If one is already running, this hands the file to it and exits, and the new Studio gets the running Vinegar's environment. Start the first Vinegar the way you want every Studio to run (see samepage merging below). Each copy of a place should be its own `.rbxl` file, as the Python pool already does on Windows.

**Don't stop one Studio with `flatpak kill org.vinegarhq.Vinegar`.** It kills every process in the app: every Studio, and any StudioMCP started through `flatpak run`. Kill the one `RobloxStudioBeta.exe` instead.

**Match processes carefully.** `pkill -f RobloxStudioBeta` also matches the shell running that command when it's run over SSH. Use a pattern that can't match itself, like `pgrep -f '[R]obloxStudioBeta.exe'`, or `pgrep -x`.

Studio's own files live in the Wine prefix:

```
~/.var/app/org.vinegarhq.Vinegar/data/vinegar/prefixes/studio/drive_c/users/<user>/AppData/Local/Roblox/
```

That includes `logs/`, `AutoSaves/`, `GlobalSettings_13.xml` and `server.rbxl`. Studio itself is in `~/.var/app/org.vinegarhq.Vinegar/data/vinegar/versions/version-<hash>/`.

## The Studio MCP

`StudioMCP.exe` ships in Studio's version folder. Run it under Vinegar's Wine as a stdio server:

```sh
V=/home/debian/.var/app/org.vinegarhq.Vinegar/data/vinegar
flatpak run --command=sh org.vinegarhq.Vinegar -c \
	"WINEDEBUG=-all WINEPREFIX=$V/prefixes/studio exec $V/kombucha/bin/wine $V/versions/version-<hash>/StudioMCP.exe"
```

It speaks MCP over stdin/stdout like on Windows, and hosts the websocket that Studios connect to (`127.0.0.1:13469` was seen). In each Studio, turn on "Enable Studio as MCP server" once. The setting persists in the prefix. One StudioMCP sees every Studio on the desktop.

To drive it from another machine, either run it over SSH as the MCP command (`ssh <server> /path/to/studio-mcp.sh`), or keep one running on the server behind a small local HTTP bridge. Remember that a `flatpak kill` of Vinegar takes this StudioMCP down too.

## Samepage merging (saves memory across copies)

Linux can merge identical memory pages across processes that opt in (KSM). Several Studios of the same version share a lot of identical memory, so with two copies this saved about 1.2 GB.

Turn the kernel scanner on (as root; the defaults are off and too slow to matter):

```sh
echo 1000 > /sys/kernel/mm/ksm/pages_to_scan
echo 20   > /sys/kernel/mm/ksm/sleep_millisecs
echo 1    > /sys/kernel/mm/ksm/run
```

A process opts in with `prctl(PR_SET_MEMORY_MERGE, 1)` (Linux 6.4+), which its children inherit. **It has to be set inside the Flatpak sandbox.** `flatpak run` starts the sandbox from a fresh process tree, so a flag set on the caller doesn't reach Studio. This launcher sets it inside and then runs the command there:

```sh
#!/bin/sh
# vinegar-ksm.sh <command> [args...]   e.g. vinegar-ksm.sh vinegar run /path/to/copy.rbxl
exec flatpak run --command=python3 org.vinegarhq.Vinegar -c '
import ctypes, os, sys
PR_SET_MEMORY_MERGE = 67
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0) != 0:
	print("vinegar-ksm: prctl failed:", os.strerror(ctypes.get_errno()), file=sys.stderr)
os.execvp(sys.argv[1], sys.argv[1:])
' "$@"
```

Because Vinegar is single-instance, stop every Vinegar first, then start the first one through this launcher. Later `vinegar run` calls hand off to it and their Studios inherit the flag.

Check it worked:

- `grep -c ' mg' /proc/<studio pid>/smaps`: non-zero means the Studio's memory is opted in.
- `/sys/kernel/mm/ksm/pages_sharing` × page size: memory saved.
- `/sys/kernel/mm/ksm/general_profit`: saving net of the scanner's own bookkeeping.

## Memory settings that apply on every platform

These come from Spike 5 and aren't Linux-specific:

- **Stop the embedded browser after the place loads (Linux).** Killing every `msedgewebview2.exe` belonging to a Studio saved 2.5–3.3 GB per Studio under Wine. On Windows a pool Studio has no browser processes, and a hand-opened one has about 0.3 GB of them. The start page, Toolbox and Assistant stop working until Studio restarts. Don't do it before login or during launch.
- **Use Run mode for server-only playtests.** `game:GetService("StudioTestService"):ExecuteRunModeAsync({})` in its own thread from `execute_luau` on the Edit DataModel, stopped with `start_stop_play`. No client, and about half the memory Play adds. Don't use `RunService:Run()`: it adds no memory, but `:Stop()` doesn't put the place back.
- **Restart Studios that have done many playtests.** Each Play start/stop leaves about 45 MB behind.
