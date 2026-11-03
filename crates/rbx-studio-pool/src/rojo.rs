use std::net::{Ipv4Addr, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use process_tree::ProcessGuard;
use thiserror::Error;
use tokio::process::Command;
use tracing::info;

use crate::platform;

const PROJECT_FILE: &str = "default.project.json";
const PORT_CHECK_TIMEOUT: Duration = Duration::from_millis(500);
const ROJO_API_PATH: &str = "/api/rojo";
pub const ROJO_PLUGIN_FILE_NAME: &str = "RspRojo.rbxm";

#[derive(Debug, Error)]
pub enum RojoError {
    #[error("{0} not found in the worktree")]
    MissingProject(PathBuf),
    #[error("failed to start `rojo serve`: {0}")]
    Spawn(#[from] process_tree::SpawnError),
    #[error("`rojo serve` did not become ready: {0}")]
    NotReady(#[from] process_tree::PortError),
    #[error("port {0} is already in use by another process")]
    PortInUse(u16),
    #[error(
        "`rojo serve` exited before it became ready (port {0} may be owned by another process)"
    )]
    ExitedEarly(u16),
    #[error("failed to check the `rojo serve` process: {0}")]
    Liveness(std::io::Error),
    #[error("failed to install the Rojo plugin: {0}")]
    Install(#[from] std::io::Error),
}

/// Which worktree a slot syncs from, and the port its `rojo serve` listens on.
#[derive(Debug, Clone)]
pub struct RojoTarget {
    pub worktree: PathBuf,
    pub port: u16,
}

/// A running `rojo serve`. Dropping it kills the process tree.
#[derive(Debug)]
pub struct RojoServer {
    pub port: u16,
    guard: ProcessGuard,
}

fn serve_args(port: u16) -> Vec<String> {
    vec![
        "serve".to_owned(),
        PROJECT_FILE.to_owned(),
        "--port".to_owned(),
        port.to_string(),
    ]
}

impl RojoServer {
    pub fn start(worktree: &Path, port: u16) -> Result<Self, RojoError> {
        let project = worktree.join(PROJECT_FILE);
        if !project.is_file() {
            return Err(RojoError::MissingProject(project));
        }
        let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
        if TcpStream::connect_timeout(&address, PORT_CHECK_TIMEOUT).is_ok() {
            return Err(RojoError::PortInUse(port));
        }
        let mut command = Command::new("rojo");
        command.args(serve_args(port)).current_dir(worktree);
        let guard = process_tree::spawn(command)?;
        info!(port, worktree = %worktree.display(), pid = ?guard.pid(), "started rojo serve");
        Ok(Self { port, guard })
    }

    #[must_use]
    pub fn pid(&self) -> Option<u32> {
        self.guard.pid()
    }

    pub async fn wait_ready(&mut self, timeout: Duration) -> Result<(), RojoError> {
        process_tree::wait_for_port(self.port, ROJO_API_PATH, timeout).await?;
        if self.guard.has_exited().map_err(RojoError::Liveness)? {
            return Err(RojoError::ExitedEarly(self.port));
        }
        Ok(())
    }
}

/// Copies a built `RspRojo.rbxm` into the Studio plugins folder. Studios must be restarted to pick it up.
pub fn install_rsp_rojo_plugin(built: &Path) -> Result<PathBuf, RojoError> {
    let dir = platform::plugins_dir();
    std::fs::create_dir_all(&dir)?;
    let target = dir.join(ROJO_PLUGIN_FILE_NAME);
    std::fs::copy(built, &target)?;
    info!(target = %target.display(), "installed rsp Rojo plugin");
    Ok(target)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serve_args_use_the_project_file_and_port() {
        assert_eq!(
            serve_args(34901),
            ["serve", "default.project.json", "--port", "34901"]
        );
    }

    #[test]
    fn start_rejects_a_port_that_is_already_listening() {
        let project = tempfile::tempdir().unwrap();
        std::fs::write(project.path().join(PROJECT_FILE), "{}").unwrap();
        let listener = std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(matches!(
            RojoServer::start(project.path(), port),
            Err(RojoError::PortInUse(found)) if found == port
        ));
    }

    #[test]
    fn start_rejects_a_worktree_without_a_project_file() {
        let empty = tempfile::tempdir().unwrap();
        assert!(matches!(
            RojoServer::start(empty.path(), 34999),
            Err(RojoError::MissingProject(_))
        ));
    }
}
