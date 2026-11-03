use std::borrow::Cow;

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, ErrorData,
    ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::{ServerHandler, ServiceExt};
use serde_json::{Map, Value};
use tracing::warn;

use crate::{StudioError, StudioId, StudioMcp};

const LIST_STUDIOS_TOOL: &str = "list_roblox_studios";
const STUDIO_ID_FIELD: &str = "studio_id";
const INPUT_SCHEMA_FIELD: &str = "inputSchema";
const NAME_FIELD: &str = "name";
const PROPERTIES_FIELD: &str = "properties";
const REQUIRED_FIELD: &str = "required";
const PROXY_INSTRUCTIONS: &str =
    "Roblox Studio tools bound to one Studio instance; studio_id is filled in automatically.";

/// Removes `studio_id` from a tool input schema's `properties` and `required`.
#[must_use]
pub fn strip_studio_id(mut schema: Value) -> Value {
    if let Some(properties) = schema
        .get_mut(PROPERTIES_FIELD)
        .and_then(Value::as_object_mut)
    {
        properties.remove(STUDIO_ID_FIELD);
    }
    if let Some(required) = schema.get_mut(REQUIRED_FIELD).and_then(Value::as_array_mut) {
        required.retain(|field| field.as_str() != Some(STUDIO_ID_FIELD));
    }
    schema
}

/// Sets `studio_id` in tool arguments, overriding any value the caller supplied.
#[must_use]
pub fn inject_studio_id(args: Value, id: &StudioId) -> Value {
    let mut map = match args {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    map.insert(STUDIO_ID_FIELD.to_string(), Value::String(id.0.clone()));
    Value::Object(map)
}

/// Drops `list_roblox_studios` and hides `studio_id` from every remaining tool's schema.
fn proxied_tools(tools: Vec<Value>) -> Vec<Value> {
    tools
        .into_iter()
        .filter(|tool| tool.get(NAME_FIELD).and_then(Value::as_str) != Some(LIST_STUDIOS_TOOL))
        .map(|mut tool| {
            if let Some(schema) = tool.get_mut(INPUT_SCHEMA_FIELD) {
                *schema = strip_studio_id(schema.take());
            }
            tool
        })
        .collect()
}

/// An MCP server that exposes the Studio tools bound to a single Studio instance.
#[derive(Debug)]
pub struct StudioProxy {
    mcp: StudioMcp,
    studio: StudioId,
}

impl StudioProxy {
    #[must_use]
    pub fn new(mcp: StudioMcp, studio: StudioId) -> Self {
        Self { mcp, studio }
    }
}

fn internal(error: &StudioError) -> ErrorData {
    ErrorData::internal_error(error.to_string(), None)
}

impl ServerHandler for StudioProxy {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_instructions(PROXY_INSTRUCTIONS)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        let tools = self
            .mcp
            .list_tools()
            .await
            .map_err(|error| internal(&error))?;
        let mut listed = ListToolsResult::default();
        for tool in proxied_tools(tools) {
            match serde_json::from_value::<Tool>(tool) {
                Ok(tool) => listed.tools.push(tool),
                Err(error) => warn!(%error, "skipping a Studio tool the proxy could not re-encode"),
            }
        }
        Ok(listed)
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        if request.name == LIST_STUDIOS_TOOL {
            return Err(ErrorData::invalid_params(
                Cow::Borrowed("unknown tool: list_roblox_studios"),
                None,
            ));
        }
        let arguments = inject_studio_id(
            request.arguments.map_or(Value::Null, Value::Object),
            &self.studio,
        );
        match self.mcp.call_tool(&request.name, arguments).await {
            Ok(value) => {
                let result: CallToolResult = serde_json::from_value(value)
                    .map_err(|error| ErrorData::internal_error(error.to_string(), None))?;
                Ok(result.into())
            }
            Err(StudioError::Tool { message, .. }) => {
                Ok(CallToolResult::error(vec![ContentBlock::text(message)]).into())
            }
            Err(error) => Err(internal(&error)),
        }
    }
}

/// Serves the proxy over stdio until the client disconnects. stdout is the MCP channel.
pub async fn serve_proxy_stdio(mcp: StudioMcp, studio: StudioId) -> Result<(), StudioError> {
    let service = StudioProxy::new(mcp, studio)
        .serve(rmcp::transport::stdio())
        .await
        .map_err(|error| StudioError::Connect(error.to_string()))?;
    service
        .waiting()
        .await
        .map_err(|error| StudioError::Connect(error.to_string()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    const EXECUTE_LUAU_SCHEMA: &str = include_str!("../fixtures/execute_luau_schema.json");

    fn execute_luau_schema() -> Value {
        serde_json::from_str(EXECUTE_LUAU_SCHEMA).expect("fixture is valid json")
    }

    #[test]
    fn strip_removes_property_and_required() {
        let stripped = strip_studio_id(execute_luau_schema());

        assert!(stripped["properties"].get("studio_id").is_none());
        assert!(stripped["properties"].get("code").is_some());
        assert_eq!(stripped["required"], json!(["code", "datamodel_type"]));
    }

    #[test]
    fn inject_overrides_caller_value() {
        let injected = inject_studio_id(
            json!({ "code": "x", "studio_id": "other" }),
            &StudioId("mine".to_string()),
        );

        assert_eq!(injected, json!({ "code": "x", "studio_id": "mine" }));
    }

    #[test]
    fn inject_creates_arguments_when_absent() {
        let injected = inject_studio_id(Value::Null, &StudioId("mine".to_string()));

        assert_eq!(injected, json!({ "studio_id": "mine" }));
    }

    #[test]
    fn list_studios_not_exposed() {
        let tools = vec![
            json!({ "name": "list_roblox_studios", "inputSchema": { "type": "object" } }),
            json!({ "name": "execute_luau", "inputSchema": execute_luau_schema() }),
        ];

        let proxied = proxied_tools(tools);

        assert_eq!(proxied.len(), 1);
        assert_eq!(proxied[0]["name"], "execute_luau");
        assert!(
            proxied[0]["inputSchema"]["properties"]
                .get("studio_id")
                .is_none()
        );
    }
}
