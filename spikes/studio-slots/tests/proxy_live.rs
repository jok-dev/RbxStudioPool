#![allow(clippy::expect_used)]

use std::time::Duration;

use rbx_studio_pool::{SlotId, SlotPool, StudioMcp};
use rmcp::model::CallToolRequestParams;
use rmcp::service::RunningService;
use rmcp::transport::TokioChildProcess;
use rmcp::{RoleClient, ServiceExt};
use serde_json::json;
use tokio::process::Command;

const EXECUTE_LUAU_TOOL: &str = "execute_luau";
const LIST_STUDIOS_TOOL: &str = "list_roblox_studios";
const READ_SLOT_CODE: &str =
    "return tostring(game:GetService(\"CoreGui\"):GetAttribute(\"RspSlot\"))";
const SETTLE: Duration = Duration::from_secs(5);

fn sandbox_ids() -> (u64, u64) {
    let read = |name: &str| -> u64 {
        std::env::var(name)
            .unwrap_or_else(|_| panic!("set {name}"))
            .parse()
            .unwrap_or_else(|_| panic!("{name} must be a number"))
    };
    (
        read("RSP_SANDBOX_PLACE_ID"),
        read("RSP_SANDBOX_UNIVERSE_ID"),
    )
}

async fn connect_proxy(slot: u8, studio_id: &str) -> RunningService<RoleClient, ()> {
    let mut command = Command::new(env!("CARGO_BIN_EXE_studio-slots"));
    command.args([
        "proxy",
        "--slot",
        &slot.to_string(),
        "--studio-id",
        studio_id,
    ]);
    ().serve(TokioChildProcess::new(command).expect("spawn proxy"))
        .await
        .expect("initialize proxy")
}

async fn read_slot_through(client: &RunningService<RoleClient, ()>) -> String {
    let tools = client.list_all_tools().await.expect("list tools");
    assert!(tools.iter().all(|tool| tool.name != LIST_STUDIOS_TOOL));
    let mut params = CallToolRequestParams::new(EXECUTE_LUAU_TOOL.to_string());
    params.arguments = json!({ "code": READ_SLOT_CODE, "datamodel_type": "Edit" })
        .as_object()
        .cloned();
    let result = client.call_tool(params).await.expect("call execute_luau");
    serde_json::to_value(&result).expect("json")["content"][0]["text"]
        .as_str()
        .unwrap_or_default()
        .trim()
        .to_string()
}

#[tokio::test]
#[ignore = "needs Studio + sandbox place"]
async fn proxy_routes_one_slot() {
    let (place, universe) = sandbox_ids();
    let pool = SlotPool::new(StudioMcp::connect().await.expect("connect"));
    let slot = pool
        .open(SlotId(1), place, universe, None)
        .await
        .expect("open slot 1");
    let client = connect_proxy(1, &slot.studio.0).await;
    assert_eq!(read_slot_through(&client).await, "1");
    drop(client);
    pool.close(slot).await.expect("close slot 1");
}

#[tokio::test]
#[ignore = "needs Studio + sandbox place"]
async fn two_proxies_route_concurrently() {
    let (place, universe) = sandbox_ids();
    let pool = SlotPool::new(StudioMcp::connect().await.expect("connect"));
    let first = pool
        .open(SlotId(1), place, universe, None)
        .await
        .expect("open slot 1");
    let second = pool
        .open(SlotId(2), place, universe, None)
        .await
        .expect("open slot 2");
    let first_client = connect_proxy(1, &first.studio.0).await;
    let second_client = connect_proxy(2, &second.studio.0).await;
    assert_eq!(read_slot_through(&first_client).await, "1");
    assert_eq!(read_slot_through(&second_client).await, "2");
    drop((first_client, second_client));
    pool.close(first).await.expect("close slot 1");
    pool.close(second).await.expect("close slot 2");
    tokio::time::sleep(SETTLE).await;
}
