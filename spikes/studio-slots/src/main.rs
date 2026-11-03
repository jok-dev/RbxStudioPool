//! Spike 1b: open and close Studio slots by hand.

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

use rbx_studio_pool::{
    RojoTarget, Slot, SlotId, SlotPool, StudioId, StudioMcp, install_rsp_rojo_plugin,
    serve_proxy_stdio,
};
use tokio::io::{AsyncBufReadExt, BufReader};
use tracing_subscriber::EnvFilter;

#[derive(Parser)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Copy the built RspRojo.rbxm into the Studio plugins folder (restart Studios afterwards).
    InstallRojoPlugin {
        #[arg(default_value = "target/RspRojo.rbxm")]
        built: PathBuf,
    },
    /// Open one slot, print its identity, and close it when Enter is pressed (or the process is killed).
    Open {
        slot: u8,
        #[arg(long)]
        place: u64,
        #[arg(long)]
        universe: u64,
        /// Worktree to run `rojo serve` in; connects the slot to it with no clicks.
        #[arg(long, requires = "port")]
        worktree: Option<PathBuf>,
        #[arg(long, requires = "worktree")]
        port: Option<u16>,
    },
    /// Open slots 1 and 2, print their identities, and close both when Enter is pressed.
    Two {
        #[arg(long)]
        place: u64,
        #[arg(long)]
        universe: u64,
    },
    /// Serve the Studio MCP tools over stdio, bound to one Studio (`studio_id` is injected).
    Proxy {
        #[arg(long)]
        slot: u8,
        #[arg(long)]
        studio_id: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_writer(std::io::stderr)
        .init();
    let command = Cli::parse().command;
    if let Command::InstallRojoPlugin { built } = &command {
        let installed = install_rsp_rojo_plugin(built).context("installing the plugin")?;
        println!("installed {}", installed.display());
        return Ok(());
    }
    if let Command::Proxy { slot, studio_id } = command {
        tracing::info!(slot, studio_id, "proxying Studio MCP");
        let mcp = StudioMcp::connect()
            .await
            .context("connecting to the Studio MCP server")?;
        return serve_proxy_stdio(mcp, StudioId(studio_id))
            .await
            .context("serving the proxy");
    }
    let pool = SlotPool::new(
        StudioMcp::connect()
            .await
            .context("connecting to the Studio MCP server")?,
    );
    match command {
        Command::InstallRojoPlugin { .. } | Command::Proxy { .. } => {}
        Command::Open {
            slot,
            place,
            universe,
            worktree,
            port,
        } => {
            let rojo = worktree
                .zip(port)
                .map(|(worktree, port)| RojoTarget { worktree, port });
            let opened = open(&pool, slot, place, universe, rojo).await?;
            wait_for_enter().await?;
            pool.close(opened).await.context("closing slot")?;
        }
        Command::Two { place, universe } => {
            let first = open(&pool, 1, place, universe, None).await?;
            let second = open(&pool, 2, place, universe, None).await?;
            wait_for_enter().await?;
            pool.close(first).await.context("closing slot 1")?;
            pool.close(second).await.context("closing slot 2")?;
        }
    }
    Ok(())
}

async fn open(
    pool: &SlotPool,
    slot: u8,
    place: u64,
    universe: u64,
    rojo: Option<RojoTarget>,
) -> Result<Slot> {
    let started = std::time::Instant::now();
    let opened = pool
        .open(SlotId(slot), place, universe, rojo)
        .await
        .with_context(|| format!("opening slot {slot}"))?;
    println!(
        "slot {slot}: studio {} pid {:?} opened in {:?}",
        opened.studio.0,
        opened.pid,
        started.elapsed()
    );
    Ok(opened)
}

async fn wait_for_enter() -> Result<()> {
    println!("press Enter to close");
    BufReader::new(tokio::io::stdin())
        .lines()
        .next_line()
        .await
        .context("reading stdin")?;
    Ok(())
}
