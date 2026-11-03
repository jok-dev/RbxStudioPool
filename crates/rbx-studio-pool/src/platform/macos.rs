use std::io;
use std::path::PathBuf;

use tokio::process::Command;

use super::PlatformError;

const STUDIO_APP: &str = "/Applications/RobloxStudio.app";

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
}

#[must_use]
pub fn studio_mcp_command() -> Command {
    Command::new(format!("{STUDIO_APP}/Contents/MacOS/StudioMCP"))
}

pub fn studio_exe() -> Result<PathBuf, PlatformError> {
    Err(PlatformError::Unsupported("launching Studio on macOS"))
}

#[must_use]
pub fn plugins_dir() -> PathBuf {
    home().join("Documents/Roblox/Plugins")
}

#[must_use]
pub fn recovery_dir() -> Option<PathBuf> {
    None
}

pub fn close_window(_pid: u32) -> io::Result<()> {
    Err(io::Error::other(PlatformError::Unsupported(
        "closing a Studio window on macOS",
    )))
}
