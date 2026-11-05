"""Pool Studios on a hidden Win32 desktop, so they can never take the user's focus.

Focus is per desktop: Studio forces itself to the foreground (on launch, ~10 times while loading, and on every
Play start), but only on the desktop it runs on. The user views the pool desktop with a hotkey (both ways), a
`rsp show`, or the bar on the pool desktop; the daemon switches back to the user's desktop when it exits.
Virtual desktops (Win+Ctrl+arrows) don't help: they share one Win32 desktop, so focus still crosses.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import queue
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("rsp")
user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
user32.CreateDesktopW.restype = wt.HANDLE
user32.OpenDesktopW.restype = wt.HANDLE
user32.OpenInputDesktop.restype = wt.HANDLE
kernel32.OpenProcess.restype = wt.HANDLE
user32.LoadImageW.restype = wt.HANDLE
user32.SendMessageW.restype = ctypes.c_void_p
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_icons = set()  # icon handles set_window_icon loaded

DESKTOP_NAME = "rbx-studio-pool"
GENERIC_ALL = 0x10000000
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_F12 = 0x7B
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY = (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F12)
HOTKEY_TEXT = "Ctrl+Alt+F12"
ICON = Path(__file__).resolve().parents[2] / "assets" / "icon.ico"  # made from assets/icon.svg
STILL_ACTIVE = 259


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class HiddenProcess:
    """The parts of subprocess.Popen the pool uses (pid, poll, returncode) for a process made with CreateProcessW."""

    def __init__(self, pi):
        self.pid = pi.dwProcessId
        self._handle = pi.hProcess
        kernel32.CloseHandle(pi.hThread)
        self.returncode = None

    def poll(self):
        if self.returncode is None:
            code = wt.DWORD()
            if kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                self.returncode = code.value
                kernel32.CloseHandle(self._handle)
        return self.returncode


def desktop_name(handle):
    buf, needed = ctypes.create_unicode_buffer(256), wt.DWORD()
    user32.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))  # UOI_NAME
    return buf.value


class PoolDesktop:
    def __init__(self, status=None):
        """status: callable returning lines for the bar (one per Studio)."""
        self.status = status or (lambda: [])
        self.user = user32.OpenInputDesktop(0, False, GENERIC_ALL)  # the desktop the user is on now
        self.user_name = desktop_name(self.user) if self.user else "Default"
        self.handle = user32.CreateDesktopW(DESKTOP_NAME, None, None, 0, GENERIC_ALL, None)
        if not self.handle:
            raise OSError(f"CreateDesktop failed ({ctypes.GetLastError()})")
        self._threads = []
        # its own taskbar identity, so the taskbar button shows the pool's icon instead of grouping under python.exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("rbx-studio-pool")
        self._front = queue.Queue()  # pids for the bar thread (on the pool desktop) to bring to the front
        self.hotkey_registered = None
        self._start(self._hotkey_loop, self.user)
        self._start(self._bar, self.handle)
        self._start(self._taskbar_button, self.user)
        log.info("pool desktop %r ready; %s switches between it and %r", DESKTOP_NAME, HOTKEY_TEXT, self.user_name)

    # --- launching ---

    def launch(self, args, cwd=None):
        """args: a list, or an already quoted command line (e.g. one copied from another process)."""
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = f"WinSta0\\{DESKTOP_NAME}"
        pi = PROCESS_INFORMATION()
        cmdline = args if isinstance(args, str) else subprocess.list2cmdline(args)
        if not kernel32.CreateProcessW(None, ctypes.create_unicode_buffer(cmdline), None, None,
                                       False, 0, None, cwd, ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(f"CreateProcess failed ({ctypes.GetLastError()})")
        return HiddenProcess(pi)

    # --- switching ---

    def showing(self):
        current = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not current:
            return False
        try:
            return desktop_name(current) == DESKTOP_NAME
        finally:
            user32.CloseDesktop(current)

    def show(self, pid=None):
        """Switch the screen to the pool desktop; with pid, that process's window comes to the front there."""
        if not user32.SwitchDesktop(self.handle):
            raise OSError(f"SwitchDesktop failed ({ctypes.GetLastError()})")
        if pid:
            self._front.put(pid)

    def hide(self):
        if self.user and not user32.SwitchDesktop(self.user):
            raise OSError(f"SwitchDesktop back failed ({ctypes.GetLastError()})")

    def close(self):
        if self.showing():
            self.hide()
        for tid in self._threads:
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    # --- threads living on a desktop ---

    def _start(self, target, desk, *args):
        ready = threading.Event()

        def run():
            if not user32.SetThreadDesktop(desk):  # must happen before the thread owns any window or hook
                log.warning("SetThreadDesktop failed (%d) for %s", ctypes.GetLastError(), target.__name__)
            self._threads.append(kernel32.GetCurrentThreadId())
            ready.set()
            target(*args)

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)

    def _hotkey_loop(self):
        """Hotkeys are per session, not per desktop (a second registration fails with 1409), so one registration
        toggles both ways. No window: WM_HOTKEY arrives on this thread's queue."""
        self.hotkey_registered = bool(user32.RegisterHotKey(None, 1, *HOTKEY))
        if not self.hotkey_registered:
            log.warning("could not register %s (%d); use `rsp show/hide` or the bar", HOTKEY_TEXT, ctypes.GetLastError())
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.hide() if self.showing() else self.show()
                except OSError as e:
                    log.warning("hotkey switch failed: %s", e)

    def _bar(self):
        """An always-on-top bar on the pool desktop, standing in for the taskbar (the desktop has no Explorer):
        one button per Studio to bring it to the front, and a way back to the user's desktop."""
        import tkinter as tk

        root = tk.Tk()
        root.title("rbx-studio-pool")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{root.winfo_screenwidth()}x30+0+{root.winfo_screenheight() - 30}")
        root.configure(bg="#1e1e1e")
        tk.Button(root, text=f"Back to Windows ({HOTKEY_TEXT})", command=lambda: self.hide(), bg="#3a6ea5",
                  fg="white", activebackground="#4a7eb5", relief="flat", padx=10).pack(side="left", fill="y")
        studios = tk.Frame(root, bg="#1e1e1e")
        studios.pack(side="left", fill="both", expand=True, padx=6)
        shown = None

        def refresh():
            nonlocal shown
            try:
                entries = [(e["label"], e.get("pid")) for e in self.status()]
            except Exception as e:
                entries = [(f"status error: {e}", None)]
            if entries != shown:  # rebuild only on change, so a click is not lost to a redraw
                shown = entries
                for child in studios.winfo_children():
                    child.destroy()
                if not entries:
                    tk.Label(studios, text="no Studios", bg="#1e1e1e", fg="#888888").pack(side="left")
                for label, pid in entries:
                    tk.Button(studios, text=label, command=lambda pid=pid: bring_to_front(pid), bg="#2d2d2d",
                              fg="#dddddd", activebackground="#3d3d3d", relief="flat", padx=8,
                              state="normal" if pid else "disabled").pack(side="left", fill="y", padx=2)
            root.after(2000, refresh)

        def take_requests():  # SetForegroundWindow only works for windows on the calling thread's desktop
            while not self._front.empty():
                bring_to_front(self._front.get_nowait(), claim_input=True)
            root.after(100, take_requests)

        refresh()
        take_requests()
        root.mainloop()

    def _taskbar_button(self):
        """A window on the user's desktop that only ever sits minimized, so the pool shows in the taskbar like an
        app. Clicking it restores the window; that switches to the pool desktop and minimizes it again. It must stay
        minimized: on the way back Windows re-activates the desktop's last active window, and an open one would
        switch straight back to the pool."""
        import tkinter as tk

        root = tk.Tk()
        root.geometry("1x1+0+0")
        root.protocol("WM_DELETE_WINDOW", root.iconify)  # closing it from the taskbar just minimizes it
        armed = False  # the first <Map> is the window being created, not a click

        def clicked(event):
            if event.widget is not root or not armed:
                return
            root.iconify()
            try:
                self.show()
            except OSError as e:
                log.warning("taskbar switch failed: %s", e)

        def arm():
            nonlocal armed
            armed = True

        def refresh():
            try:
                n = len(self.status())
            except Exception:
                n = "?"
            title = f"rbx-studio-pool ({n} running, {HOTKEY_TEXT})"
            if root.title() != title:
                root.title(title)
            set_window_icon(int(root.wm_frame(), 16))  # only acts while the window has no icon of ours
            root.after(2000, refresh)

        root.bind("<Map>", clicked)
        root.iconify()
        root.after(1000, arm)
        refresh()
        root.mainloop()


def set_window_icon(hwnd):
    """Gives a window the pool's icon, big (taskbar) and small (title bar), straight from the .ico: tk's own icon
    calls left the taskbar button on tk's feather."""
    LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON, WM_GETICON = 0x10, 1, 0x0080, 0x007F
    if not ICON.is_file() or user32.SendMessageW(hwnd, WM_GETICON, 1, 0) in _icons:
        return
    # ICON_BIG at SM_CXICON, ICON_SMALL at SM_CXSMICON: the .ico's frame of that size (they follow display scaling)
    for which, metric in ((1, 11), (0, 49)):
        size = user32.GetSystemMetrics(metric)
        icon = user32.LoadImageW(None, str(ICON), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if icon:
            _icons.add(icon)
            user32.SendMessageW(hwnd, WM_SETICON, which, icon)
        else:
            log.warning("could not load %s (%d)", ICON, ctypes.GetLastError())


def studio_window(pid):
    """A process's main window on the calling thread's desktop: the visible top-level window with the longest title
    (Studio's has the place name in it)."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def each(hwnd, _):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found.append((len(title.value), hwnd))
        return True

    user32.EnumWindows(each, 0)
    return max(found)[1] if found else None


def bring_to_front(pid, claim_input=False):
    """From a bar click this thread may set the foreground window. Without a click (claim_input), a synthetic Alt
    tap first makes this process the last one to receive input, which Windows requires for SetForegroundWindow."""
    hwnd = pid and studio_window(pid)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if claim_input:
        user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
        user32.keybd_event(0x12, 0, 2, 0)  # up
    ok = bool(user32.SetForegroundWindow(hwnd))
    if not ok:
        log.warning("could not bring pid %d to the front", pid)
    return ok
