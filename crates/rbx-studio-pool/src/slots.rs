use std::time::Duration;

use process_tree::ProcessGuard;
use tokio::process::Command;
use tokio::sync::Mutex;
use tracing::{info, warn};

use crate::platform;
use crate::{
    LaunchError, RojoServer, RojoTarget, StudioError, StudioId, StudioInfo, StudioMcp,
    new_studio_id,
};

const POLL_INTERVAL: Duration = Duration::from_secs(2);
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(180);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(30);
const LIST_STUDIOS_CALL_TIMEOUT: Duration = Duration::from_secs(15);
const FIRST_LUAU_RETRY_INTERVAL: Duration = Duration::from_secs(2);
const FIRST_LUAU_TIMEOUT: Duration = Duration::from_secs(60);
const LUAU_CALL_TIMEOUT: Duration = Duration::from_secs(15);
const ROJO_READY_TIMEOUT: Duration = Duration::from_secs(30);
const MARKER_ATTRIBUTE: &str = "RspSlot";
const ROJO_PORT_ATTRIBUTE: &str = "RspRojoPort";
const IDENTITY_SEPARATOR: char = '|';
const IDENTITY_UNCLAIMED: &str = "nil";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct SlotId(pub u8);

/// An open Studio. Dropping it kills the launched process tree.
#[derive(Debug)]
pub struct Slot {
    pub id: SlotId,
    pub studio: StudioId,
    pub pid: Option<u32>,
    launched: ProcessGuard,
    /// Declared after `launched`, so Studio is killed before its Rojo server.
    pub rojo: Option<RojoServer>,
}

/// Opens and closes Studios. Launches are serialized so a list diff always sees exactly one new Studio.
#[derive(Debug)]
pub struct SlotPool {
    launch_lock: Mutex<()>,
    mcp: StudioMcp,
}

impl SlotPool {
    #[must_use]
    pub fn new(mcp: StudioMcp) -> Self {
        Self {
            launch_lock: Mutex::new(()),
            mcp,
        }
    }

    #[must_use]
    pub fn mcp(&self) -> &StudioMcp {
        &self.mcp
    }

    pub async fn open(
        &self,
        slot: SlotId,
        place: u64,
        universe: u64,
        rojo: Option<RojoTarget>,
    ) -> Result<Slot, LaunchError> {
        let rojo_server = match &rojo {
            Some(target) => {
                let mut server = RojoServer::start(&target.worktree, target.port)?;
                server.wait_ready(ROJO_READY_TIMEOUT).await?;
                Some(server)
            }
            None => None,
        };
        let _launching = self.launch_lock.lock().await;
        let before = self.mcp.list_studios().await?;
        let mut command = Command::new(platform::studio_exe()?);
        command.args([
            "-task",
            "EditPlace",
            "-placeId",
            &place.to_string(),
            "-universeId",
            &universe.to_string(),
        ]);
        let launched = process_tree::spawn(command)?;
        let pid = launched.pid();
        info!(slot = slot.0, place, universe, ?pid, "launched Studio");
        let studio = self.wait_for_new_studio(&before).await?;
        self.verify_identity(&studio, place).await?;
        let marker = format!(
            "game:GetService(\"CoreGui\"):SetAttribute(\"{MARKER_ATTRIBUTE}\", {})",
            slot.0
        );
        self.mcp.execute_luau(&studio, &marker).await?;
        let actual = self.read_marker(&studio).await?;
        if actual != slot.0.to_string() {
            return Err(LaunchError::MarkerMismatch {
                expected: slot.0,
                actual,
            });
        }
        if let Some(target) = &rojo {
            let attribute = format!(
                "game:GetService(\"CoreGui\"):SetAttribute(\"{ROJO_PORT_ATTRIBUTE}\", {})",
                target.port
            );
            self.mcp.execute_luau(&studio, &attribute).await?;
        }
        Ok(Slot {
            id: slot,
            studio,
            pid,
            launched,
            rojo: rojo_server,
        })
    }

    /// Confirms the Studio is the sandbox place and not already claimed by another slot.
    /// The first Luau call can fail while the `DataModel` loads, so it is retried.
    async fn verify_identity(&self, studio: &StudioId, place: u64) -> Result<(), LaunchError> {
        let code = format!(
            "return tostring(game.PlaceId) .. \"{IDENTITY_SEPARATOR}\" .. tostring(game:GetService(\"CoreGui\"):GetAttribute(\"{MARKER_ATTRIBUTE}\"))"
        );
        let deadline = tokio::time::Instant::now() + FIRST_LUAU_TIMEOUT;
        let output = loop {
            let attempt =
                tokio::time::timeout(LUAU_CALL_TIMEOUT, self.mcp.execute_luau(studio, &code)).await;
            match attempt {
                Ok(Ok(output)) => break output,
                Ok(Err(error)) => warn!(%error, "first Luau call failed, retrying"),
                Err(_) => warn!("first Luau call timed out, retrying"),
            }
            if tokio::time::Instant::now() >= deadline {
                return Err(LaunchError::NotResponding(FIRST_LUAU_TIMEOUT));
            }
            tokio::time::sleep(FIRST_LUAU_RETRY_INTERVAL).await;
        };
        check_identity(studio, &output, place)
    }

    pub async fn read_marker(&self, studio: &StudioId) -> Result<String, StudioError> {
        let code = format!(
            "return tostring(game:GetService(\"CoreGui\"):GetAttribute(\"{MARKER_ATTRIBUTE}\"))"
        );
        Ok(self
            .mcp
            .execute_luau(studio, &code)
            .await?
            .trim()
            .to_string())
    }

    /// Asks the window to close and waits for the process to exit; if it does not, the launch guard kills it on drop.
    pub async fn close(&self, mut slot: Slot) -> Result<(), StudioError> {
        let Some(pid) = slot.pid else {
            warn!(slot = slot.id.0, "no pid known, killing the process");
            return Ok(());
        };
        if let Err(error) = platform::close_window(pid) {
            warn!(slot = slot.id.0, pid, %error, "graceful close failed, killing the process");
            return Ok(());
        }
        if tokio::time::timeout(CLOSE_TIMEOUT, slot.launched.wait())
            .await
            .is_err()
        {
            warn!(
                slot = slot.id.0,
                pid, "Studio did not exit after close, killing the process"
            );
        }
        Ok(())
    }

    async fn wait_for_new_studio(&self, before: &[StudioInfo]) -> Result<StudioId, LaunchError> {
        let deadline = tokio::time::Instant::now() + LAUNCH_TIMEOUT;
        loop {
            tokio::time::sleep(POLL_INTERVAL).await;
            let after = match tokio::time::timeout(
                LIST_STUDIOS_CALL_TIMEOUT,
                self.mcp.list_studios(),
            )
            .await
            {
                Ok(Ok(after)) => after,
                Ok(Err(error)) => {
                    warn!(%error, "list_studios failed while waiting for Studio, retrying");
                    if tokio::time::Instant::now() >= deadline {
                        return Err(LaunchError::Timeout(LAUNCH_TIMEOUT));
                    }
                    continue;
                }
                Err(_) => {
                    warn!("list_studios timed out while waiting for Studio, retrying");
                    if tokio::time::Instant::now() >= deadline {
                        return Err(LaunchError::Timeout(LAUNCH_TIMEOUT));
                    }
                    continue;
                }
            };
            match new_studio_id(before, &after) {
                Err(LaunchError::NoNewStudio) if tokio::time::Instant::now() < deadline => {}
                Err(LaunchError::NoNewStudio) => return Err(LaunchError::Timeout(LAUNCH_TIMEOUT)),
                other => return other,
            }
        }
    }
}

/// Parses the `PlaceId|claimedSlot` probe output and rejects Studios that are not ours to use.
fn check_identity(studio: &StudioId, output: &str, expected_place: u64) -> Result<(), LaunchError> {
    let reject = |reason: String| LaunchError::NotOurStudio {
        studio: studio.clone(),
        reason,
    };
    let Some((place, claimed)) = output.trim().split_once(IDENTITY_SEPARATOR) else {
        return Err(reject(format!("unreadable identity probe `{output}`")));
    };
    if place != expected_place.to_string() {
        return Err(reject(format!(
            "place id is {place}, expected {expected_place}"
        )));
    }
    if claimed != IDENTITY_UNCLAIMED {
        return Err(reject(format!("already claimed by slot {claimed}")));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn studio() -> StudioId {
        StudioId("s".into())
    }

    #[test]
    fn matching_unclaimed_studio_is_accepted() {
        assert!(
            check_identity(
                &studio(),
                "123|nil
",
                123
            )
            .is_ok()
        );
    }

    #[test]
    fn wrong_place_is_rejected() {
        assert!(matches!(
            check_identity(&studio(), "999|nil", 123),
            Err(LaunchError::NotOurStudio { .. })
        ));
    }

    #[test]
    fn claimed_studio_is_rejected() {
        assert!(matches!(
            check_identity(&studio(), "123|2", 123),
            Err(LaunchError::NotOurStudio { .. })
        ));
    }

    #[test]
    fn garbage_is_rejected() {
        assert!(matches!(
            check_identity(&studio(), "oops", 123),
            Err(LaunchError::NotOurStudio { .. })
        ));
    }
}
