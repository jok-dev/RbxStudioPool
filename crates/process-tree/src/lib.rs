//! Process spawning with whole-tree kill-on-drop, plus a readiness probe for local HTTP services.

use std::io;
use std::process::ExitStatus;
use std::time::Duration;

#[cfg(windows)]
use process_wrap::tokio::JobObject;
#[cfg(unix)]
use process_wrap::tokio::ProcessGroup;
use process_wrap::tokio::{ChildWrapper, CommandWrap, KillOnDrop};
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::process::{ChildStdout, Command};
use tracing::debug;

const PORT_PROBE_INTERVAL: Duration = Duration::from_millis(100);
const PORT_PROBE_ATTEMPT_TIMEOUT: Duration = Duration::from_secs(2);
const HTTP_STATUS_PREFIX_LEN: usize = 12;

#[derive(Debug, Error)]
pub enum SpawnError {
    #[error("failed to spawn process: {0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Error)]
pub enum PortError {
    #[error("port {port} did not answer GET {path} within {timeout:?}")]
    Timeout {
        port: u16,
        path: String,
        timeout: Duration,
    },
}

/// A running child process. Dropping it kills the child and everything it spawned
/// (job object on Windows, process group on Unix).
#[derive(Debug)]
pub struct ProcessGuard {
    child: Box<dyn ChildWrapper>,
}

pub fn spawn(cmd: Command) -> Result<ProcessGuard, SpawnError> {
    let mut wrap = CommandWrap::from(cmd);
    #[cfg(windows)]
    wrap.wrap(JobObject);
    #[cfg(unix)]
    wrap.wrap(ProcessGroup::leader());
    wrap.wrap(KillOnDrop);
    let child = wrap.spawn()?;
    Ok(ProcessGuard { child })
}

impl ProcessGuard {
    pub fn stdout(&mut self) -> Option<ChildStdout> {
        self.child.stdout().take()
    }

    pub async fn wait(&mut self) -> io::Result<ExitStatus> {
        self.child.wait().await
    }

    /// Non-blocking check for whether the child has already exited.
    pub fn has_exited(&mut self) -> io::Result<bool> {
        Ok(self.child.try_wait()?.is_some())
    }

    #[must_use]
    pub fn pid(&self) -> Option<u32> {
        self.child.id()
    }
}

/// Polls `GET http://127.0.0.1:{port}{path}` until it answers with a 2xx status or `timeout` passes.
pub async fn wait_for_port(port: u16, path: &str, timeout: Duration) -> Result<(), PortError> {
    let probe = async {
        loop {
            match tokio::time::timeout(PORT_PROBE_ATTEMPT_TIMEOUT, probe_once(port, path)).await {
                Ok(Ok(true)) => return,
                Ok(Ok(false)) => debug!(port, path, "port answered with a non-2xx status"),
                Ok(Err(error)) => debug!(port, path, %error, "port probe failed"),
                Err(_) => debug!(port, path, "port probe attempt timed out"),
            }
            tokio::time::sleep(PORT_PROBE_INTERVAL).await;
        }
    };
    tokio::time::timeout(timeout, probe)
        .await
        .map_err(|_| PortError::Timeout {
            port,
            path: path.to_owned(),
            timeout,
        })
}

async fn probe_once(port: u16, path: &str) -> io::Result<bool> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).await?;
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).await?;
    let mut prefix = [0u8; HTTP_STATUS_PREFIX_LEN];
    stream.read_exact(&mut prefix).await?;
    // "HTTP/1.1 200" -> status digits start at byte 9.
    Ok(prefix.starts_with(b"HTTP/1.") && prefix[9] == b'2')
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    const POLL_ATTEMPTS: u32 = 50;
    const POLL_INTERVAL: Duration = Duration::from_millis(100);

    async fn serve_http(listener: TcpListener, status_line: &'static str) {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            let mut buffer = [0u8; 512];
            let _ = stream.read(&mut buffer).await;
            let response =
                format!("{status_line}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            let _ = stream.write_all(response.as_bytes()).await;
        }
    }

    #[tokio::test]
    async fn wait_for_port_succeeds_on_http_200() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(serve_http(listener, "HTTP/1.1 200 OK"));
        wait_for_port(port, "/", Duration::from_secs(5))
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn wait_for_port_times_out_on_unused_port() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let result = wait_for_port(port, "/", Duration::from_millis(400)).await;
        assert!(matches!(result, Err(PortError::Timeout { .. })));
    }

    #[tokio::test]
    async fn wait_for_port_times_out_on_non_2xx() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        tokio::spawn(serve_http(listener, "HTTP/1.1 503 Service Unavailable"));
        let result = wait_for_port(port, "/", Duration::from_millis(400)).await;
        assert!(matches!(result, Err(PortError::Timeout { .. })));
    }

    #[cfg(unix)]
    fn tree_command() -> Command {
        let mut cmd = Command::new("sh");
        cmd.args(["-c", "sleep 30 & echo $!; wait"]);
        cmd
    }

    #[cfg(windows)]
    fn tree_command() -> Command {
        let mut cmd = Command::new("powershell");
        cmd.args([
            "-NoProfile",
            "-Command",
            "$p = Start-Process ping -ArgumentList '-n','30','127.0.0.1' -PassThru -WindowStyle Hidden; $p.Id; Start-Sleep 30",
        ]);
        cmd
    }

    #[cfg(unix)]
    fn is_alive(pid: u32) -> bool {
        std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    #[cfg(windows)]
    fn is_alive(pid: u32) -> bool {
        std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH"])
            .output()
            .is_ok_and(|output| String::from_utf8_lossy(&output.stdout).contains(&pid.to_string()))
    }

    async fn wait_until_dead(pid: u32) -> bool {
        for _ in 0..POLL_ATTEMPTS {
            if !is_alive(pid) {
                return true;
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
        false
    }

    #[tokio::test]
    async fn has_exited_reports_running_then_exited() {
        let mut running = spawn(tree_command()).unwrap();
        assert!(!running.has_exited().unwrap());
        drop(running);

        let mut finished = spawn(exit_command()).unwrap();
        finished.wait().await.unwrap();
        assert!(finished.has_exited().unwrap());
    }

    #[cfg(unix)]
    fn exit_command() -> Command {
        Command::new("true")
    }

    #[cfg(windows)]
    fn exit_command() -> Command {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "exit 0"]);
        cmd
    }

    #[tokio::test]
    async fn drop_kills_child_tree() {
        use tokio::io::{AsyncBufReadExt, BufReader};

        let mut cmd = tree_command();
        cmd.stdout(std::process::Stdio::piped());
        let mut guard = spawn(cmd).unwrap();
        let child_pid = guard.pid().unwrap();
        let stdout = guard.stdout().unwrap();
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line).await.unwrap();
        let grandchild_pid: u32 = line.trim().parse().unwrap();
        assert!(is_alive(child_pid));
        assert!(is_alive(grandchild_pid));

        drop(guard);

        assert!(wait_until_dead(child_pid).await, "child survived drop");
        assert!(
            wait_until_dead(grandchild_pid).await,
            "grandchild survived drop"
        );
    }
}
