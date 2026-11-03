use std::time::Duration;

use std::path::PathBuf;

use rbx_studio_pool::{RojoServer, RojoTarget, SlotId, SlotPool, StudioMcp};

const STUDIO_SETTLE: Duration = Duration::from_secs(5);
const ROJO_READY_TIMEOUT: Duration = Duration::from_secs(30);
const ROJO_PORT_CLOSE_WAIT: Duration = Duration::from_secs(2);
const ROJO_TEST_PORT: u16 = 34990;
const ROJO_PORT_A: u16 = 34901;
const ROJO_PORT_B: u16 = 34902;
const SYNC_WAIT: Duration = Duration::from_secs(15);

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

#[tokio::test]
#[ignore = "needs Studio MCP"]
async fn lists_connected_studios() {
    let mcp = StudioMcp::connect().await.expect("connect");
    let studios = mcp.list_studios().await.expect("list studios");
    println!("{} studio(s)", studios.len());
    for studio in &studios {
        assert!(!studio.id.0.is_empty());
        assert!(!studio.name.is_empty());
    }
}

#[tokio::test]
#[ignore = "needs Studio + sandbox place"]
async fn opens_marks_and_closes_one_slot() {
    let (place, universe) = sandbox_ids();
    let pool = SlotPool::new(StudioMcp::connect().await.expect("connect"));
    let slot = pool
        .open(SlotId(1), place, universe, None)
        .await
        .expect("open slot 1");
    assert_eq!(
        pool.read_marker(&slot.studio).await.expect("read marker"),
        "1"
    );
    pool.close(slot).await.expect("close slot 1");
    tokio::time::sleep(STUDIO_SETTLE).await;
    let reopened = pool
        .open(SlotId(1), place, universe, None)
        .await
        .expect("reopen slot 1");
    pool.close(reopened).await.expect("close reopened slot");
}

#[tokio::test]
#[ignore = "needs Studio + sandbox place"]
async fn two_slots_are_distinct_and_isolated() {
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
    assert_ne!(first.studio, second.studio);
    assert_eq!(
        pool.read_marker(&first.studio).await.expect("marker 1"),
        "1"
    );
    assert_eq!(
        pool.read_marker(&second.studio).await.expect("marker 2"),
        "2"
    );
    pool.mcp()
        .execute_luau(&first.studio, "local part = Instance.new('Part'); part.Name = 'SpikeMarker1'; part.Parent = workspace")
        .await
        .expect("create marker part in slot 1");
    let in_second = pool
        .mcp()
        .execute_luau(
            &second.studio,
            "return tostring(workspace:FindFirstChild('SpikeMarker1') ~= nil)",
        )
        .await
        .expect("look for marker part in slot 2");
    assert_eq!(in_second.trim(), "false");
    pool.close(first).await.expect("close slot 1");
    pool.close(second).await.expect("close slot 2");
}

fn worktree(name: &str) -> PathBuf {
    PathBuf::from(std::env::var(name).unwrap_or_else(|_| panic!("set {name}")))
}

#[tokio::test]
#[ignore = "needs rojo + a game checkout"]
async fn rojo_serve_starts_and_dies_with_its_handle() {
    let mut server =
        RojoServer::start(&worktree("RSP_GAME_WORKTREE"), ROJO_TEST_PORT).expect("start rojo");
    server
        .wait_ready(ROJO_READY_TIMEOUT)
        .await
        .expect("rojo ready");
    drop(server);
    tokio::time::sleep(ROJO_PORT_CLOSE_WAIT).await;
    let closed =
        process_tree::wait_for_port(ROJO_TEST_PORT, "/api/rojo", Duration::from_millis(500))
            .await
            .is_err();
    assert!(closed, "rojo kept answering after drop");
}

#[tokio::test]
#[ignore = "needs 2 Studios + sandbox place + 2 game worktrees (A has a local edit)"]
async fn two_slots_sync_from_their_own_worktrees() {
    let (place, universe) = sandbox_ids();
    let module_path = std::env::var("RSP_SYNC_MODULE_PATH")
        .expect("set RSP_SYNC_MODULE_PATH to a game.* path of the module edited in worktree A");
    let pool = SlotPool::new(StudioMcp::connect().await.expect("connect"));
    let target = |name: &str, port: u16| RojoTarget {
        worktree: worktree(name),
        port,
    };
    let first = pool
        .open(
            SlotId(1),
            place,
            universe,
            Some(target("RSP_GAME_WORKTREE_A", ROJO_PORT_A)),
        )
        .await
        .expect("open slot 1");
    let second = pool
        .open(
            SlotId(2),
            place,
            universe,
            Some(target("RSP_GAME_WORKTREE_B", ROJO_PORT_B)),
        )
        .await
        .expect("open slot 2");
    tokio::time::sleep(SYNC_WAIT).await;
    let read = format!("return {module_path}.Source");
    let source_a = pool
        .mcp()
        .execute_luau(&first.studio, &read)
        .await
        .expect("read A");
    let source_b = pool
        .mcp()
        .execute_luau(&second.studio, &read)
        .await
        .expect("read B");
    assert_ne!(source_a, source_b, "slots synced the same source");
    pool.close(first).await.expect("close slot 1");
    pool.close(second).await.expect("close slot 2");
}
