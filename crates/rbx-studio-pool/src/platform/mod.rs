//! All OS-specific paths, executables and window calls.

use thiserror::Error;

#[cfg(target_os = "macos")]
mod macos;
#[cfg(windows)]
mod windows;

#[cfg(target_os = "macos")]
pub use macos::{close_window, plugins_dir, recovery_dir, studio_exe, studio_mcp_command};
#[cfg(windows)]
pub use windows::{close_window, plugins_dir, recovery_dir, studio_exe, studio_mcp_command};

#[cfg(not(any(windows, target_os = "macos")))]
compile_error!("rbx-studio-pool supports Windows and macOS only");

#[derive(Debug, Error)]
pub enum PlatformError {
    #[error("not supported on this platform yet: {0}")]
    Unsupported(&'static str),
    #[error("could not locate {0}")]
    NotFound(&'static str),
}
