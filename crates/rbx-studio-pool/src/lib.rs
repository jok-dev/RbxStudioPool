//! Everything touching Studio: MCP client, launcher, slot pool, Rojo, proxy.

mod error;
mod launcher;
mod mcp_client;
pub mod platform;
mod proxy;
mod rojo;
mod slots;

pub use error::{LaunchError, StudioError};
pub use launcher::new_studio_id;
pub use mcp_client::{StudioId, StudioInfo, StudioMcp};
pub use proxy::{StudioProxy, inject_studio_id, serve_proxy_stdio, strip_studio_id};
pub use rojo::{ROJO_PLUGIN_FILE_NAME, RojoError, RojoServer, RojoTarget, install_rsp_rojo_plugin};
pub use slots::{Slot, SlotId, SlotPool};
