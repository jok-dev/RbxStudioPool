use std::io;
use std::path::PathBuf;
use std::time::SystemTime;

use tokio::process::Command;
use windows_sys::Win32::Foundation::{HWND, LPARAM, TRUE};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetWindowThreadProcessId, IsWindowVisible, PostMessageW, WM_CLOSE,
};

use super::PlatformError;

const STUDIO_EXE_NAME: &str = "RobloxStudioBeta.exe";

fn local_app_data() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_default()
}

#[must_use]
pub fn studio_mcp_command() -> Command {
    let mut command = Command::new("cmd");
    command
        .arg("/c")
        .arg(local_app_data().join("Roblox").join("mcp.bat"));
    command
}

/// The newest installed Studio build that contains the exe.
pub fn studio_exe() -> Result<PathBuf, PlatformError> {
    let versions = local_app_data().join("Roblox").join("Versions");
    let mut newest: Option<(SystemTime, PathBuf)> = None;
    for entry in std::fs::read_dir(&versions).into_iter().flatten().flatten() {
        let exe = entry.path().join(STUDIO_EXE_NAME);
        let Ok(modified) = exe.metadata().and_then(|meta| meta.modified()) else {
            continue;
        };
        if newest.as_ref().is_none_or(|(best, _)| modified > *best) {
            newest = Some((modified, exe));
        }
    }
    newest
        .map(|(_, exe)| exe)
        .ok_or(PlatformError::NotFound(STUDIO_EXE_NAME))
}

#[must_use]
pub fn plugins_dir() -> PathBuf {
    local_app_data().join("Roblox").join("Plugins")
}

/// Where Studio keeps autosave/recovery files; not located yet on Windows.
#[must_use]
pub fn recovery_dir() -> Option<PathBuf> {
    None
}

struct CloseRequest {
    pid: u32,
    posted: bool,
}

#[allow(unsafe_code)]
unsafe extern "system" fn post_close_to_process_window(window: HWND, param: LPARAM) -> i32 {
    // SAFETY: `param` is the address of the `CloseRequest` that `close_window` keeps alive for the whole `EnumWindows` call.
    let request = unsafe { &mut *(param as *mut CloseRequest) };
    let mut window_pid = 0u32;
    // SAFETY: `window` is a handle supplied by EnumWindows and `window_pid` is a valid out pointer.
    unsafe { GetWindowThreadProcessId(window, &raw mut window_pid) };
    // SAFETY: `window` is a handle supplied by EnumWindows.
    if window_pid == request.pid && unsafe { IsWindowVisible(window) } != 0 {
        // SAFETY: posting a message to a window handle has no memory-safety preconditions.
        request.posted |= unsafe { PostMessageW(window, WM_CLOSE, 0, 0) } != 0;
    }
    TRUE
}

/// Asks every visible top-level window of `pid` to close, like clicking the X.
#[allow(unsafe_code)]
pub fn close_window(pid: u32) -> io::Result<()> {
    let mut request = CloseRequest { pid, posted: false };
    // SAFETY: the callback only dereferences the pointer to `request`, which outlives this synchronous call.
    unsafe {
        EnumWindows(
            Some(post_close_to_process_window),
            std::ptr::from_mut(&mut request) as LPARAM,
        )
    };
    if request.posted {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("no visible window found for pid {pid}"),
        ))
    }
}
