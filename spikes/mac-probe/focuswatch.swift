// Focus helper for probe 5.
//   focuswatch watch          print every app activation: "<unix time> <pid> <name>", the current front app first
//   focuswatch hide PID       hide that app (NSRunningApplication.hide)
//   focuswatch activate PID   bring that app to the front
import AppKit

func line(_ app: NSRunningApplication?) {
    guard let app = app else { return }
    print(String(format: "%.3f", Date().timeIntervalSince1970), app.processIdentifier, app.localizedName ?? "?")
    fflush(stdout)
}

let args = CommandLine.arguments
switch args.count > 1 ? args[1] : "" {
case "watch":
    line(NSWorkspace.shared.frontmostApplication)
    NSWorkspace.shared.notificationCenter.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
    ) { note in line(note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication) }
    RunLoop.main.run()
case "hide", "activate":
    guard args.count > 2, let pid = Int32(args[2]), let app = NSRunningApplication(processIdentifier: pid) else {
        FileHandle.standardError.write("no such app\n".data(using: .utf8)!); exit(1)
    }
    let ok = args[1] == "hide" ? app.hide() : app.activate()
    exit(ok ? 0 : 2)
default:
    FileHandle.standardError.write("usage: focuswatch watch | hide PID | activate PID\n".data(using: .utf8)!); exit(64)
}
