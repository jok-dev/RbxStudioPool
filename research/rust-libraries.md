# Rust libraries for the daemon

Research for [#2](https://github.com/jok-dev/RbxStudioPool/issues/2), gathered 2026-11-06. It covers the crates that replace the Python spike's stdlib, `keyring`, PowerShell CIM, `ps`/`pgrep` and `ctypes` calls (`spikes/python-pool/`). Nothing was added to the workspace.

Sources: the crates.io API (`/api/v1/crates/<name>` and `/versions`: latest version, date, licence, declared `rust-version`, downloads), each crate's GitHub repo (last push, stars, README, `Cargo.toml`, source), and docs.rs signatures. "Downloads" is all-time / last 90 days as crates.io reports them. The workspace constraints that matter here: MSRV **1.90** (`rust-version` in `Cargo.toml`, toolchain pinned to 1.98.1), `unsafe_code = deny` with FFI only in platform modules ([#16](https://github.com/jok-dev/RbxStudioPool/issues/16)), and a Linux CI job that builds the core with no backend ([#17](https://github.com/jok-dev/RbxStudioPool/issues/17)).

## Summary

| Need | Recommendation | Unsafe left in our code |
|---|---|---|
| Local HTTP server (daemon API) | **axum 0.8** on the existing tokio | none |
| HTTP client (CLI → daemon, Open Cloud) | **reqwest 0.13**, features `json`, `gzip`, `stream` | none |
| Headerless gzip from the CDN | **flate2 1.1** (miniz_oxide backend) | none |
| OS keychain | **keyring-core 1** + **windows-native-keyring-store 1.1** + **apple-native-keyring-store 1.0** (`keychain`) | none |
| File watching | **notify 8.2** + **notify-debouncer-full 0.7**, with `PollWatcher` as a fallback; **globset** for `src/**/*.lua[u]` | none |
| TOML (`pool.toml`) | **toml 1.x** with serde | none |
| Process inspection (command lines, children) | **sysinfo**, minimal features; pin **0.38** or raise MSRV to 1.95 for 0.39+ | none |
| Win32 (pool desktop, windows, hotkey, taskbar) | **windows-sys 0.61** (already used); no safe crate covers these calls | every call, in `platform/windows` |
| AppKit (hide on activate, unhide, activate) | **objc2-app-kit 0.3** + **objc2-foundation 0.3** | the notification observer and the notification-name statics only |

Two version traps came out of this: **sysinfo 0.39+ needs Rust 1.95**, above the workspace MSRV, and **`windows`/`windows-sys` 0.100 will be a breaking rewrite** (see [Win32](#win32-bindings-windows-sys-vs-windows)).

## Local HTTP server and client

The spike's daemon is `http.server.ThreadingHTTPServer` on `127.0.0.1:7811` with JSON bodies, and the CLI uses `urllib.request` with a 900s timeout (`rsp.py`). [#14](https://github.com/jok-dev/RbxStudioPool/issues/14) keeps one local API for the CLI and MCP fronts.

| Crate | Latest | Licence | Downloads | Repo activity | MSRV |
|---|---|---|---|---|---|
| [axum](https://github.com/tokio-rs/axum) | 0.8.9 (2026-04-14) | MIT | 571M / 154M | pushed 2026-11-03, 27.7k stars | 1.80 |
| [hyper](https://github.com/hyperium/hyper) | 1.12.0 (2026-10-06) | MIT | 1.09B / 269M | pushed 2026-11-02, 16.4k stars | 1.63 |
| [reqwest](https://github.com/seanmonstar/reqwest) | 0.13.5 (2026-09-08) | MIT OR Apache-2.0 | 880M / 251M | pushed 2026-10-19, 11.9k stars | 1.85 |
| [ureq](https://github.com/algesten/ureq) | 3.5.1 (2026-11-02) | MIT OR Apache-2.0 | 256M / 82M | pushed 2026-11-02, 2.2k stars | 1.85 |

**Server: axum.** It is the tokio project's router over hyper 1 and tower, so it adds no runtime. Raw hyper works but means writing routing, JSON extraction and error mapping by hand for ~25 verbs. axum is all safe code from our side. rmcp 3.8 itself uses axum 0.8 + hyper 1 for its `transport-streamable-http-server` feature ([rmcp `Cargo.toml`](https://github.com/modelcontextprotocol/rust-sdk/blob/main/crates/rmcp/Cargo.toml)), so if the MCP front ever serves HTTP it shares the stack. Pure Rust; Windows, macOS and Linux alike.

**Client: reqwest** for both the CLI → daemon calls and Open Cloud. rmcp also depends on reqwest 0.13 for its HTTP client transports. ureq 3 is the lighter, blocking alternative (no tokio). It would suit a CLI that never needs async, but it means a second HTTP stack next to the daemon's, so reqwest wins on "one stack".

reqwest 0.13 defaults ([`Cargo.toml`](https://github.com/seanmonstar/reqwest/blob/master/Cargo.toml)): `default = ["default-tls", "charset", "http2", "system-proxy"]`, `default-tls = ["rustls"]`, and `rustls` pulls **aws-lc-rs** plus `rustls-platform-verifier` (OS trust store on Windows and macOS). aws-lc-rs needs a C compiler but, per its [README](https://github.com/aws/aws-lc-rs/blob/main/aws-lc-rs/README.md), "CMake is **never** required" and "Bindgen is **never** required" for non-FIPS builds, so MSVC (already needed by Rust on Windows) and Xcode CLT suffice. `native-tls` (SChannel / Security.framework) is the alternative if the C build ever bites. The local daemon calls are plain `http://127.0.0.1`, so TLS only matters for Open Cloud.

## Open Cloud Asset Delivery client

The spike (`cloudplace.py`) does two requests: `GET apis.roblox.com/asset-delivery-api/v1/assetId/{id}` with `x-api-key`, which returns JSON with a `location` CDN URL; then a plain `GET location` with `Accept-Encoding: gzip`. It decompresses when `Content-Encoding` says gzip, and **also** when the body starts with `1f 8b` without the header saying so, then checks the result starts with `<roblox`.

- **reqwest** with `json` (parse the `location` reply), `gzip` (transparent decoding when `Content-Encoding: gzip`; in 0.13 it maps to `tower-http/decompression-gzip`) and `stream` (write the place to `.part` in chunks rather than buffering a large file).
- **flate2 1.1.10** (2026-08-28, MIT OR Apache-2.0, 822M downloads, MSRV 1.67, [rust-lang/flate2-rs](https://github.com/rust-lang/flate2-rs)) for the headerless-gzip fallback: sniff the first two bytes of the finished file and gunzip with `GzDecoder`. The default `miniz_oxide` backend is pure Rust. (`async-compression` 0.4.52 is the async alternative, but a post-download sniff doesn't need it.)
- **Redirects:** reqwest follows up to 10 by default. On a cross-host redirect it strips only `Authorization`, `Cookie`, `Cookie2`, `Proxy-Authorization` and `WWW-Authenticate` ([`src/redirect.rs` `remove_sensitive_headers`](https://github.com/seanmonstar/reqwest/blob/master/src/redirect.rs)). **`x-api-key` is not stripped.** So set the key per request on the `locate` call only, never in the client's `default_headers`, and fetch the CDN URL in a separate request without it, as the spike does.

No unsafe in our code. Linux: same code.

## OS keychain

The spike uses Python `keyring` with service `rbx-studio-pool` and user `roblox-api-key` (`cloudplace.py`).

| Crate | Latest | Licence | Downloads | Repo | MSRV |
|---|---|---|---|---|---|
| [keyring](https://github.com/open-source-cooperative/keyring-rs) | 4.2.0 (2026-08-29) | MIT OR Apache-2.0 | 37M / 18M | pushed 2026-11-05, 779 stars | 1.88 |
| [keyring-core](https://github.com/open-source-cooperative/keyring-core) | 1.0.1 (2026-10-13) | MIT OR Apache-2.0 | 6.9M / 5.7M | pushed 2026-11-03 | 1.85 |
| [windows-native-keyring-store](https://github.com/open-source-cooperative/windows-native-keyring-store) | 1.1.0 (2026-05-24) | MIT OR Apache-2.0 | 3.8M / 3.0M | pushed 2026-11-04 | 1.88 |
| [apple-native-keyring-store](https://github.com/open-source-cooperative/apple-native-keyring-store) | 1.0.2 (2026-08-06) | MIT OR Apache-2.0 | 3.7M / 3.0M | pushed 2026-11-04 | 1.85 |
| [security-framework](https://github.com/kornelski/rust-security-framework) | 3.7.0 (2026-02-20) | MIT OR Apache-2.0 | 437M / 110M | pushed 2026-08-14 | 1.85 |

**keyring has split.** Per the keyring-rs [README](https://github.com/open-source-cooperative/keyring-rs), the `keyring` crate is now a wrapper: its default `v1` feature keeps the old API and links the Apple, Windows **and** zbus Secret Service stores. Applications "should take dependencies on the keyring-core crate and the specific credential stores they want". The [`Cargo.toml`](https://github.com/open-source-cooperative/keyring-rs/blob/main/Cargo.toml) confirms `v1 = [apple-native-keyring-store/keychain, windows-native-keyring-store, zbus-secret-service-keyring-store]`.

Recommendation: **keyring-core** in the core, with the store picked per platform in `cfg`-gated deps:

- Windows: **windows-native-keyring-store** (Credential Manager; built on `windows-sys = "0.61.2"`, the same version the workspace uses). Its README warns that Credential Manager "loses updates when credentials ... are written and deleted concurrently", so the store serialises its calls with a named mutex keyed by the user's SID. Disable its default `search` feature to drop `regex`.
- macOS: **apple-native-keyring-store** with feature `keychain` (wraps `security-framework` 3.7; `protected` is the Data Protection keychain, which needs an entitled, signed app).
- Linux later: one of the ecosystem's stores (`zbus-secret-service-keyring-store`, `dbus-secret-service-keyring-store`, `linux-keyutils-keyring-store`), all listed in keyring-rs's `cli` feature. That is just a new `cfg` arm.

**Compatibility with keys the spike already stored.** The Windows store builds a credential's `target_name` as prefix + user + delimiter + service + suffix, with defaults `""`, `"."`, `""` ([`src/lib.rs` docs](https://github.com/open-source-cooperative/windows-native-keyring-store/blob/main/src/lib.rs)), so a default entry would be `roblox-api-key.rbx-studio-pool`. An explicit `target` modifier overrides that. To read a key the Python spike stored, open the entry with an explicit `target` matching the Python layout. I believe Python's `WinVaultKeyring` uses the service as the target name, but I did not verify that here: check one stored credential in Credential Manager before relying on it, or simply ask users to run `cloud-key set` again. On macOS both use a generic-password item keyed by service + account.

All unsafe (Credential Manager FFI, Security.framework) stays inside these crates. None in ours.

## File watching

The spike polls every second: mounted folders, plus `src/**/*.lua[u]` with `--restart-on-code` (README findings "Live config in playtests" and "Restart on code change").

| Crate | Latest | Licence | Downloads | MSRV |
|---|---|---|---|---|
| [notify](https://github.com/notify-rs/notify) | 8.2.1 (2026-10-25) | **CC0-1.0** | 190M / 54M | 1.85 |
| notify-debouncer-full | 0.7.0 (2026-01-23) | MIT OR Apache-2.0 | 20M / 6.3M | 1.85 |
| notify-debouncer-mini | 0.7.0 (2025-08-03) | MIT OR Apache-2.0 | 18M / 5.1M | 1.77 |
| [globset](https://github.com/BurntSushi/ripgrep/tree/master/crates/globset) | 0.4.21 (2026-10-27) | Unlicense OR MIT | 284M / 71M | 1.88 |

The notify repo was pushed 2026-11-05 and has 3.5k stars. Its README lists users including rust-analyzer, deno, mdBook, watchexec and zed. Backends: ReadDirectoryChangesW on Windows, FSEvents on macOS (kqueue optional), inotify on Linux, and polling on every platform.

**Recommendation: notify + notify-debouncer-full**, filtering events through a `globset` matcher for `src/**/*.lua[u]`. The full debouncer stitches rename pairs and uses file ids, which absorbs editors' save-by-rename. Edits then arrive in well under the spike's 1s polling granularity, with no per-second directory walk of a large `src` tree. Caveats from notify's own docs ([`notify/src/lib.rs`](https://github.com/notify-rs/notify/blob/main/notify/src/lib.rs)):

- "Network mounted filesystems like NFS may not emit any events". The fix is to use `PollWatcher` explicitly for those paths, whose default interval is 30s, so set it to 1s to match the spike. Mount sources are user-supplied paths, so a per-mount fallback (or a `poll = true` override) is worth keeping.
- On FSEvents, files you don't own may not report events (use `PollWatcher` then). `std::fs::copy` on APFS can fire metadata events on the *source* path, which matters if the pool copies templates out of a watched tree.
- Windows "may emit no event for a directory rename or move".
- **notify 9 is coming.** The main-branch README already links `UPGRADING_V8_TO_V9.md` and states MSRV **1.91**, one above the workspace's 1.90. Stay on 8.x until the MSRV moves.

No unsafe in our code. Linux: inotify, same API.

## TOML

`pool.toml` is frozen ([#15](https://github.com/jok-dev/RbxStudioPool/issues/15)) and the spike reads it with `tomllib` (`repo.py`).

| Crate | Latest | Licence | Downloads | MSRV |
|---|---|---|---|---|
| [toml](https://github.com/toml-rs/toml) | 1.2.0+spec-1.1.0 (2026-11-04) | MIT OR Apache-2.0 | 1.10B / 299M | 1.85 |
| toml_edit | 0.25.18 (2026-11-04) | MIT OR Apache-2.0 | 992M / 271M | 1.85 |
| basic-toml | 0.1.10 (2025-03-03) | MIT OR Apache-2.0 | 76M | — |

**toml** with serde `Deserialize` structs. It is Cargo's own TOML stack and supports TOML 1.1, a superset of the 1.0 that `tomllib` reads, so every existing `pool.toml` parses. `toml_edit` is only worth it if a settings window ever rewrites `pool.toml` and must keep its comments. basic-toml has seen no release since 2025-03. Pure Rust, no unsafe, all platforms.

## Process inspection

The spike lists a process's children with `Get-CimInstance Win32_Process -Filter "ParentProcessId=…"` (`rsp.py`), matches Studio logs to a slot by command line, and on macOS the spike used `pgrep` because `open` returns no PID (`spike/mac`: `spikes/mac-probe/README.md`). Killing whole trees stays with `process-tree` / process-wrap. The macOS spike also found that killing the server does not end its clients, so the pool must find and kill them itself.

| Crate | Latest | Licence | Downloads | Repo | MSRV |
|---|---|---|---|---|---|
| [sysinfo](https://github.com/GuillaumeGomez/sysinfo) | 0.40.1 (2026-10-30) | MIT | 240M / 60M | pushed 2026-10-30, 2.8k stars | **1.95** |
| [libproc](https://github.com/andrewdavidmackenzie/libproc-rs) | 0.15.0 (2026-11-04) | MIT | 11M / 3.9M | 82 stars | 1.85 |

**sysinfo** gives `Process::cmd() -> &[OsString]`, `parent()`, `exe()` and `name()` on Windows, macOS and Linux. Refresh only the pids you care about with `ProcessRefreshKind` and filter on `parent()` to find children. Disable default features (`component`, `disk`, `gpu`, `network`, `system`, `user`) and enable only `system`, which avoids pulling WMI/COM and IOKit. Internally it uses `windows = ">=0.62, <0.63"` (the version already in our lockfile, via process-wrap) and `objc2-core-foundation` 0.3.2 on macOS. Reading another process's command line works for processes of the same user, which covers Studio.

**MSRV trap:** every sysinfo release since 0.39.0 (2026-05-07) declares `rust-version = "1.95"`. 0.37.0 – 0.38.4 (latest 2026-03-09) declare 1.88. With resolver 3's MSRV-aware fallback and the workspace at 1.90, Cargo prefers 0.38.x. Forcing `0.40` compiles on the pinned 1.98.1 toolchain but quietly breaks the 1.90 claim. Either pin `sysinfo = "0.38"` or raise the workspace MSRV to 1.95. The windows-rs 0.100 crates also need 1.95 (below), so raising it may be the simpler call.

libproc is macOS/Linux only (no Windows) and has a far smaller user base, so it is no replacement. Without sysinfo, Windows command lines would need hand-written `NtQueryInformationProcess` + `ReadProcessMemory`, which is exactly the unsafe FFI #16 wants to avoid.

No unsafe in our code.

## Win32 bindings: windows-sys vs windows

The spike's Win32 surface (`desktop.py`, `taskbar.py`): `CreateDesktopW` / `OpenInputDesktop` / `SwitchDesktop` / `SetThreadDesktop` / `CloseDesktop`, `CreateProcessW` with `STARTUPINFOW.lpDesktop` (the pool desktop), `EnumWindows` / `GetWindowThreadProcessId` / `IsWindowVisible` / `GetWindowTextW`, `ShowWindow` / `SetForegroundWindow` / `keybd_event`, `RegisterHotKey` + `GetMessageW` loop, `PostThreadMessageW`, `SetCurrentProcessExplicitAppUserModelID`, and the taskbar button window (`CreateWindowExW`, `LoadImageW`, GDI, DWM). Job objects are already covered by process-wrap.

| Crate | Latest | Licence | Downloads | MSRV |
|---|---|---|---|---|
| [windows-sys](https://github.com/microsoft/windows-rs) | 0.61.2 (2025-10-06) | MIT OR Apache-2.0 | 1.87B / 498M | 1.71 |
| windows | 0.62.2 (2025-10-06) | MIT OR Apache-2.0 | 395M / 106M | 1.82 |

**No safe crate covers these.** Desktops, foreground control and hotkeys are raw Win32 in both crates. In `windows` 0.62 they remain `unsafe fn`, e.g. `pub unsafe fn SetForegroundWindow(hwnd: HWND) -> BOOL` and `pub unsafe fn EnumWindows(...) -> Result<()>` ([windows-docs-rs](https://microsoft.github.io/windows-docs-rs/doc/windows/Win32/UI/WindowsAndMessaging/fn.SetForegroundWindow.html)). `windows` adds `Result` wrapping, `PCWSTR`/`w!` helpers and COM interfaces, but no safety for these calls. **Stay on windows-sys 0.61**: the workspace and `platform/windows.rs` already use it, it compiles fastest, and windows-native-keyring-store uses the same version. Reach for `windows` 0.62 only if a COM API is needed (e.g. `ITaskbarList3` or `IVirtualDesktopManager`). It is already in the lockfile as a transitive dependency.

Every one of these calls is `unsafe` and belongs in `platform/windows` with SAFETY comments (#16). The one structural need is the `EnumWindows` callback (`unsafe extern "system" fn`), which `platform/windows.rs` already does.

**Upcoming churn.** windows-rs's [August 2026 newsletter (issue #4867)](https://github.com/microsoft/windows-rs/issues/4867) says the next release moves the crates to `0.100.0`, Rust 2024, and MSRV **1.95** (windows-sys 1.88). `windows`/`windows-sys` move to metadata parsed from SDK headers, so "APIs are also grouped by lowercase Windows header names rather than the old category hierarchy" and generated enum/ownership types may disappear. It also says "we may initially publish the release without `windows` and `windows-sys`". As of today crates.io has `windows-core` and `windows-window` at 0.100.0 (2026-09-03) but `windows`/`windows-sys` still at 0.61/0.62. Expect a manual migration of feature names and imports when 0.100 lands. Keeping the Win32 surface small and in one module limits the cost. The project now recommends `windows-bindgen` for libraries, which generates only the functions used. That is an option for this app too if the umbrella crates' churn hurts.

Related, for the tray / menu-bar icon ([#21](https://github.com/jok-dev/RbxStudioPool/issues/21)), which is outside this ticket's list: [tray-icon](https://github.com/tauri-apps/tray-icon) 0.26.1 (2026-10-07, MIT OR Apache-2.0, 41M downloads, MSRV 1.90) + muda 0.21.2 for menus are the maintained cross-platform crates (Tauri), and would replace a hand-written tray on both OSes.

## AppKit bindings: objc2-app-kit

On macOS the pool launches with `open -n -g -j` (std `Command`, no FFI), hides pool Studios when they activate, unhides them for `show`, and reactivates the user's previous app (Mac spike: "Hiding on activation undoes a grab").

| Crate | Latest | Licence | Downloads | MSRV |
|---|---|---|---|---|
| [objc2](https://github.com/madsmtm/objc2) | 0.6.5 (2026-10-05) | MIT | 154M / 62M | 1.71 |
| objc2-app-kit | 0.3.2 (2025-10-04) | Zlib OR Apache-2.0 OR MIT | 84M / 38M | 1.71 |
| objc2-foundation | 0.3.2 (2025-10-04) | MIT | 120M / 53M | 1.71 |

objc2 repo: pushed 2026-10-17, 1.1k stars. The framework crates are generated, and sysinfo, tray-icon/muda and winit build on them. The older `cocoa`/`objc` crates are the unmaintained predecessors.

**objc2-app-kit 0.3 makes most of what the pool needs safe** (docs.rs signatures, 0.3.2):

- Safe: `NSRunningApplication::runningApplicationWithProcessIdentifier(pid) -> Option<Retained<Self>>`, `hide()`, `unhide()`, `isActive()`, `isHidden()`, `activateWithOptions(..)`, `activateFromApplication_options(..)`, `processIdentifier()`, `bundleIdentifier()`; `NSWorkspace::sharedWorkspace()`, `frontmostApplication()`, `runningApplications()`, `notificationCenter()`.
- Still `unsafe`: registering the activation observer, because `NSNotificationCenter::addObserverForName_object_queue_usingBlock` is `pub unsafe fn` (objc2-foundation). So is reading the name constant, since `NSWorkspaceDidActivateApplicationNotification` is a `pub unsafe static`. Pulling `NSWorkspaceApplicationKey` out of the notification's `userInfo` also involves a static and a downcast. All of this is one small block in `platform/macos`.
- Notifications are delivered on a run loop. The daemon needs the main thread running an `NSApplication`/`CFRunLoop`, which the tray/menu-bar icon needs anyway. objc2's `MainThreadMarker` enforces main-thread-only APIs at compile time.

Linux: not applicable. Hide/focus control becomes the Linux backend's own problem, behind the platform trait.

## Open points for the decisions

- **MSRV 1.90 vs 1.95.** sysinfo ≥ 0.39 and the windows-rs 0.100 crates need 1.95. notify 9 needs 1.91. Raising the MSRV (the toolchain is already 1.98.1) unblocks the current versions of all three. Keeping it means pinning sysinfo 0.38 and staying on windows-sys 0.61 / notify 8.
- **TLS backend for reqwest:** default rustls + aws-lc-rs (needs a C compiler) vs `native-tls`. Both work on Windows and macOS.
- **Keychain migration:** read the spike's existing credential by explicit target, or ask users to set the key again.
