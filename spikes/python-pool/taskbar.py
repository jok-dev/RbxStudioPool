"""The pool's taskbar button on the user's desktop: a plain Win32 window that always stays minimized.

- Click: the taskbar asks a minimized window to restore (WM_SYSCOMMAND SC_RESTORE). We swallow that, so the window
  never opens, and switch to the pool desktop instead. Staying minimized matters: on the way back Windows
  re-activates the desktop's last active window, and an open one would send the user straight back to the pool.
- Hover: DWM asks for the thumbnail (WM_DWMSENDICONICTHUMBNAIL) and gets a status card drawn with GDI: the Studios
  on the pool desktop can't be captured usefully (their 3D viewports come out blank off-screen).
"""

import ctypes
import ctypes.wintypes as wt
import logging
import re

log = logging.getLogger("rsp")
user32, gdi32, kernel32, dwmapi = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32, ctypes.windll.dwmapi

LRESULT = wt.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.LoadImageW.restype = wt.HANDLE
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HANDLE, ctypes.c_int, ctypes.c_int, wt.UINT,
                              wt.HBRUSH, wt.UINT]
user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
kernel32.GetModuleHandleW.restype = wt.HMODULE
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreateFontW.restype = wt.HFONT
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
dwmapi.DwmSetIconicThumbnail.argtypes = [wt.HWND, wt.HBITMAP, wt.DWORD]
dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmInvalidateIconicBitmaps.argtypes = [wt.HWND]

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_SYSCOMMAND = 0x0002, 0x0010, 0x0113, 0x0112
WM_DWMSENDICONICTHUMBNAIL = 0x0323
SC_RESTORE, SC_MAXIMIZE, SC_CLOSE = 0xF120, 0xF030, 0xF060
WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOWMINNOACTIVE = 7
DWMWA_FORCE_ICONIC_REPRESENTATION, DWMWA_HAS_ICONIC_BITMAP, DWMWA_DISALLOW_PEEK = 7, 10, 11
DT_SINGLELINE, DT_END_ELLIPSIS, DT_NOPREFIX, DT_VCENTER = 0x20, 0x8000, 0x800, 0x4
REFRESH_MS = 2000


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


BG, PANEL, TEXT, DIM, ACCENT = rgb(20, 27, 39), rgb(31, 41, 58), rgb(232, 238, 247), rgb(140, 152, 172), rgb(95, 208, 255)


def run(status, on_click, icon_path, title_hint):
    """Creates the button and runs its message loop on the calling thread (which must be on the user's desktop).
    status(): [{"label": str, ...}] one per Studio slot; on_click(): switch to the pool desktop."""
    hinst = kernel32.GetModuleHandleW(None)
    icons = {}
    if icon_path.is_file():
        for name, metric in (("big", 11), ("small", 49), ("card", None)):  # SM_CXICON, SM_CXSMICON; 64px for the thumbnail
            size = user32.GetSystemMetrics(metric) if metric else 64
            icons[name] = user32.LoadImageW(None, str(icon_path), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
    state = {"labels": None}

    def labels():
        try:
            return [e["label"] for e in status()]
        except Exception as e:
            return [f"status error: {e}"]

    def title_for(lines):
        return f"rbx-studio-pool ({len(lines)} running, {title_hint})"

    @WNDPROC
    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_SYSCOMMAND:
                command = wparam & 0xFFF0
                if command in (SC_RESTORE, SC_MAXIMIZE):  # a click on the button or its thumbnail
                    try:
                        on_click()
                    except OSError as e:
                        log.warning("taskbar switch failed: %s", e)
                    return 0
                if command == SC_CLOSE:  # "Close window" from the taskbar: the daemon owns this window
                    return 0
            elif msg == WM_CLOSE:
                return 0
            elif msg == WM_DWMSENDICONICTHUMBNAIL:
                draw_thumbnail(hwnd, (lparam >> 16) & 0xFFFF, lparam & 0xFFFF, state["labels"] or [], icons.get("card"),
                               title_hint)
                return 0
            elif msg == WM_TIMER:
                lines = labels()
                if lines != state["labels"]:
                    state["labels"] = lines
                    user32.SetWindowTextW(hwnd, title_for(lines))
                    dwmapi.DwmInvalidateIconicBitmaps(hwnd)  # the next hover redraws the card
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("taskbar window message %#x failed", msg)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    cls = WNDCLASSEXW()
    cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
    cls.lpfnWndProc = wndproc
    cls.hInstance = hinst
    cls.hIcon = icons.get("big")
    cls.hIconSm = icons.get("small")
    cls.lpszClassName = "RbxStudioPoolTaskbar"
    if not user32.RegisterClassExW(ctypes.byref(cls)):
        log.warning("taskbar button: RegisterClassEx failed (%d)", ctypes.GetLastError())
        return
    state["labels"] = labels()
    hwnd = user32.CreateWindowExW(0, cls.lpszClassName, title_for(state["labels"]), WS_OVERLAPPEDWINDOW,
                                  0, 0, 400, 300, None, None, hinst, None)
    if not hwnd:
        log.warning("taskbar button: CreateWindowEx failed (%d)", ctypes.GetLastError())
        return
    on = wt.BOOL(True)
    for attr in (DWMWA_FORCE_ICONIC_REPRESENTATION, DWMWA_HAS_ICONIC_BITMAP, DWMWA_DISALLOW_PEEK):
        dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
    user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
    user32.SetTimer(hwnd, 1, REFRESH_MS, None)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    user32.DestroyWindow(hwnd)


def draw_thumbnail(hwnd, max_w, max_h, lines, icon, hint):
    card = render_card(max_w, max_h, lines, icon, hint)
    log.info("taskbar thumbnail: asked for at most %dx%d, drew %s", max_w, max_h, card and card[1:3])
    if not card:
        return
    bmp = card[0]
    result = dwmapi.DwmSetIconicThumbnail(hwnd, bmp, 0)
    if result:
        log.warning("DwmSetIconicThumbnail failed (%#x) for a %dx%d card (asked for at most %dx%d)",
                    result & 0xFFFFFFFF, card[1], card[2], max_w, max_h)
    gdi32.DeleteObject(bmp)


def render_card(max_w, max_h, lines, icon, hint):
    """A status card at most max_w x max_h. Returns (HBITMAP, width, height, pixel address) for the caller to use and
    delete, or None. The taskbar's box follows the monitor's shape (200x54 on a 32:9 screen), so a wide, short box
    gets a one-glance strip instead of the card."""
    w, h = max(max_w, 1), max(max_h, 1)  # never larger than asked: DWM rejects that (E_INVALIDARG)
    strip = w > h * 2.4
    if not strip and w > h * 1.6:  # keep a card shape; the taskbar centers what it gets
        w = int(h * 1.6)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down 32bpp
    bits = ctypes.c_void_p()
    dc = gdi32.CreateCompatibleDC(None)
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not bmp:
        gdi32.DeleteDC(dc)
        return None
    old = gdi32.SelectObject(dc, bmp)
    try:
        fill(dc, 0, 0, w, h, BG)
        gdi32.SetBkMode(dc, 1)  # TRANSPARENT
        (draw_strip if strip else draw_card)(dc, w, h, lines, icon, hint)
    finally:
        gdi32.SelectObject(dc, old)
    # GDI leaves alpha at 0; DWM wants an opaque premultiplied bitmap
    size = w * h * 4
    raw = bytearray((ctypes.c_ubyte * size).from_address(bits.value))
    raw[3::4] = b"\xff" * (w * h)
    ctypes.memmove(bits.value, bytes(raw), size)
    gdi32.DeleteDC(dc)
    return bmp, w, h, bits.value


def fonts_for(sizes):
    """Segoe UI fonts, one per (pixel height, weight)."""
    return [gdi32.CreateFontW(-int(px), 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, 5, 0, "Segoe UI")  # CLEARTYPE_QUALITY
            for px, weight in sizes]


def who(line):
    """The lease's client from a bar label ("slot 1: agent-a | playing" -> "agent-a")."""
    m = re.match(r"^slot \d+: ([^|]+)", line)
    return m.group(1).strip() if m else line


def draw_strip(dc, w, h, lines, icon, hint):
    """Wide and short: the icon at full height, how many Studios are running, and who has them."""
    pad = max(h // 10, 3)
    icon_size = h - pad * 2
    if icon:
        user32.DrawIconEx(dc, pad, pad, icon, icon_size, icon_size, 0, None, 3)  # DI_NORMAL
    left = pad * 2 + icon_size
    leased = [l for l in lines if "idle" not in l]
    headline = f"{len(lines)} Studio{'' if len(lines) == 1 else 's'}" if lines else "No Studios"
    detail = ", ".join(who(l) for l in leased) if leased else ("all idle" if lines else hint)
    big, small = fonts_for(((h * 0.42, 600), (h * 0.3, 400)))
    try:
        mid = int(h * 0.56)
        text(dc, big, TEXT, headline, left, 0, w - pad, mid)
        text(dc, small, ACCENT if leased else DIM, detail, left, mid - pad // 2, w - pad, h - pad // 2)
    finally:
        gdi32.DeleteObject(big)
        gdi32.DeleteObject(small)


def draw_card(dc, w, h, lines, icon, hint):
    """Header, one row per Studio slot, and how to get there."""
    unit = max(h // 8, 11)  # one text row
    pad = unit // 2
    fonts = fonts_for(((unit * 1.2, 600), (unit * 0.95, 400), (unit * 0.8, 400)))
    try:
        icon_size = int(unit * 1.4)
        if icon:
            user32.DrawIconEx(dc, pad, pad, icon, icon_size, icon_size, 0, None, 3)  # DI_NORMAL
        text(dc, fonts[0], TEXT, "rbx-studio-pool", pad * 2 + icon_size, pad, w - pad, pad + icon_size)
        y = pad * 2 + icon_size
        footer = h - unit - pad
        rows = lines or ["No Studios running"]
        row_h = int(unit * 1.3)
        shown = max(1, (footer - y - pad) // row_h)
        for i, line in enumerate(rows[:shown]):
            more = i == shown - 1 and len(rows) > shown
            fill(dc, pad, y, w - pad, y + row_h - 2, PANEL)
            if lines and not more:
                fill(dc, pad, y, pad + 3, y + row_h - 2, DIM if "idle" in line else ACCENT)  # leased: accent
            label = f"+ {len(rows) - shown + 1} more" if more else re.sub(r"^slot (\d+): ", r"\1  ", line)
            text(dc, fonts[1], TEXT if lines else DIM, label, pad + 8, y, w - pad - 4, y + row_h - 2)
            y += row_h
        text(dc, fonts[2], DIM, f"Click to open  ·  {hint}", pad, footer, w - pad, h - pad // 2)
    finally:
        for f in fonts:
            gdi32.DeleteObject(f)


def fill(dc, left, top, right, bottom, color):
    brush = gdi32.CreateSolidBrush(color)
    user32.FillRect(dc, ctypes.byref(wt.RECT(left, top, right, bottom)), brush)
    gdi32.DeleteObject(brush)


def text(dc, font, color, s, left, top, right, bottom):
    old = gdi32.SelectObject(dc, font)
    gdi32.SetTextColor(dc, color)
    user32.DrawTextW(dc, s, -1, ctypes.byref(wt.RECT(left, top, right, bottom)),
                     DT_SINGLELINE | DT_END_ELLIPSIS | DT_NOPREFIX | DT_VCENTER)
    gdi32.SelectObject(dc, old)
