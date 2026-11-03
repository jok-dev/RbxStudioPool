use std::time::Duration;

use thiserror::Error;

use crate::StudioId;
use crate::platform::PlatformError;

#[derive(Debug, Error)]
pub enum StudioError {
    #[error("failed to start the Studio MCP server: {0}")]
    Spawn(#[from] std::io::Error),
    #[error("Studio MCP connection failed: {0}")]
    Connect(String),
    #[error("Studio MCP call `{tool}` failed: {message}")]
    Call { tool: String, message: String },
    #[error("Studio MCP tool `{tool}` returned an error: {message}")]
    Tool { tool: String, message: String },
    #[error("unexpected Studio MCP response from `{tool}`: {message}")]
    Response { tool: String, message: String },
    #[error(transparent)]
    Platform(#[from] PlatformError),
}

#[derive(Debug, Error)]
pub enum LaunchError {
    #[error("no new Studio appeared after launch")]
    NoNewStudio,
    #[error("more than one new Studio appeared, cannot tell which is ours: {0:?}")]
    Ambiguous(Vec<StudioId>),
    #[error("Studio did not appear within {0:?}")]
    Timeout(Duration),
    #[error("failed to launch Studio: {0}")]
    Spawn(#[from] process_tree::SpawnError),
    #[error(transparent)]
    Studio(#[from] StudioError),
    #[error(transparent)]
    Platform(#[from] PlatformError),
    #[error("slot marker mismatch: wrote {expected}, read back `{actual}`")]
    MarkerMismatch { expected: u8, actual: String },
    #[error("refusing to use Studio {studio:?}: {reason}")]
    NotOurStudio { studio: StudioId, reason: String },
    #[error("Studio did not answer Luau within {0:?}")]
    NotResponding(Duration),
    #[error(transparent)]
    Rojo(#[from] crate::RojoError),
}
