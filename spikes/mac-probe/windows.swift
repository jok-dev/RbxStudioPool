// Lists the on-screen windows of the given PIDs: windows <pid>...
// Build: swiftc -O windows.swift -o state/windows
import CoreGraphics
import Foundation

let pids = Set(CommandLine.arguments.dropFirst().compactMap { Int32($0) })
let infos = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
for w in infos {
    guard let pid = w[kCGWindowOwnerPID as String] as? Int32, pids.contains(pid) else { continue }
    let b = w[kCGWindowBounds as String] as? [String: Double] ?? [:]
    let onscreen = w[kCGWindowIsOnscreen as String] as? Bool ?? false
    let name = w[kCGWindowName as String] as? String ?? "?"
    print("pid \(pid) layer \(w[kCGWindowLayer as String] ?? "?") onscreen \(onscreen) \(Int(b["Width"] ?? 0))x\(Int(b["Height"] ?? 0)) name \(name)")
}
