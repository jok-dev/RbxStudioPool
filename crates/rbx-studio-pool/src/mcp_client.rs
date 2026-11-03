use std::time::Duration;

#[cfg(windows)]
use process_wrap::tokio::JobObject;
#[cfg(unix)]
use process_wrap::tokio::ProcessGroup;
use process_wrap::tokio::{CommandWrap, KillOnDrop};
use rmcp::model::CallToolRequestParams;
use rmcp::service::RunningService;
use rmcp::transport::TokioChildProcess;
use rmcp::{RoleClient, ServiceExt};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::StudioError;
use crate::platform;

const LIST_STUDIOS_TOOL: &str = "list_roblox_studios";
const EXECUTE_LUAU_TOOL: &str = "execute_luau";
const EDIT_DATAMODEL: &str = "Edit";
const CONNECT_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct StudioId(pub String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StudioInfo {
    pub id: StudioId,
    pub name: String,
}

#[derive(Deserialize)]
struct ListedStudios {
    studios: Vec<ListedStudio>,
}

#[derive(Deserialize)]
struct ListedStudio {
    id: String,
    name: String,
}

/// Client for the Studio MCP server. The child process dies with this value.
pub struct StudioMcp {
    client: RunningService<RoleClient, ()>,
}

impl std::fmt::Debug for StudioMcp {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("StudioMcp").finish_non_exhaustive()
    }
}

impl StudioMcp {
    pub async fn connect() -> Result<Self, StudioError> {
        let mut wrap = CommandWrap::from(platform::studio_mcp_command());
        #[cfg(windows)]
        wrap.wrap(JobObject);
        #[cfg(unix)]
        wrap.wrap(ProcessGroup::leader());
        wrap.wrap(KillOnDrop);
        let transport = TokioChildProcess::new(wrap)?;
        let client = tokio::time::timeout(CONNECT_TIMEOUT, ().serve(transport))
            .await
            .map_err(|_| {
                StudioError::Connect(format!("no initialize response within {CONNECT_TIMEOUT:?}"))
            })?
            .map_err(|error| StudioError::Connect(error.to_string()))?;
        Ok(Self { client })
    }

    pub async fn list_studios(&self) -> Result<Vec<StudioInfo>, StudioError> {
        let result = self.call_tool(LIST_STUDIOS_TOOL, json!({})).await?;
        let text = first_text(&result);
        let listed: ListedStudios =
            serde_json::from_str(&text).map_err(|error| StudioError::Response {
                tool: LIST_STUDIOS_TOOL.to_string(),
                message: format!("{error}: {text}"),
            })?;
        Ok(listed
            .studios
            .into_iter()
            .map(|studio| StudioInfo {
                id: StudioId(studio.id),
                name: studio.name,
            })
            .collect())
    }

    /// Runs `code` in the Edit datamodel of `id` and returns the tool's text output.
    pub async fn execute_luau(&self, id: &StudioId, code: &str) -> Result<String, StudioError> {
        let arguments =
            json!({ "code": code, "datamodel_type": EDIT_DATAMODEL, "studio_id": id.0 });
        let result = self.call_tool(EXECUTE_LUAU_TOOL, arguments).await?;
        Ok(first_text(&result))
    }

    pub async fn list_tools(&self) -> Result<Vec<Value>, StudioError> {
        let tools = self
            .client
            .list_all_tools()
            .await
            .map_err(|error| StudioError::Call {
                tool: "tools/list".to_string(),
                message: error.to_string(),
            })?;
        tools
            .iter()
            .map(|tool| {
                serde_json::to_value(tool).map_err(|error| StudioError::Response {
                    tool: "tools/list".to_string(),
                    message: error.to_string(),
                })
            })
            .collect()
    }

    /// Raw passthrough: returns the full `CallToolResult` JSON, failing if the tool reported `isError`.
    pub async fn call_tool(&self, name: &str, args: Value) -> Result<Value, StudioError> {
        let mut params = CallToolRequestParams::new(name.to_string());
        if let Value::Object(map) = args {
            params.arguments = Some(map);
        }
        let result = self
            .client
            .call_tool(params)
            .await
            .map_err(|error| StudioError::Call {
                tool: name.to_string(),
                message: error.to_string(),
            })?;
        let value = serde_json::to_value(&result).map_err(|error| StudioError::Response {
            tool: name.to_string(),
            message: error.to_string(),
        })?;
        if value
            .get("isError")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            return Err(StudioError::Tool {
                tool: name.to_string(),
                message: first_text(&value),
            });
        }
        Ok(value)
    }
}

fn first_text(result: &Value) -> String {
    result
        .get("content")
        .and_then(Value::as_array)
        .and_then(|blocks| {
            blocks
                .iter()
                .find_map(|block| block.get("text").and_then(Value::as_str))
        })
        .unwrap_or_default()
        .to_string()
}
