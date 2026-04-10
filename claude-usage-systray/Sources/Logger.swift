import Foundation
import os.log

/// Centralized logger that writes to both os_log (visible in Console.app)
/// and a rotating log file at ~/Library/Logs/ClaudeUsageSystray/app.log.
enum AppLogger {
    private static let subsystem = "com.claude-usage-systray"

    static let general = os.Logger(subsystem: subsystem, category: "general")
    static let api     = os.Logger(subsystem: subsystem, category: "api")
    static let engine  = os.Logger(subsystem: subsystem, category: "engine")

    private static let logDir: URL = {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/ClaudeUsageSystray")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()

    private static let logFile: URL = logDir.appendingPathComponent("app.log")

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    private static let fileLock = NSLock()

    /// Write a log entry to both os_log and the log file.
    static func log(_ level: OSLogType = .default, category: String, _ message: String) {
        let logger = os.Logger(subsystem: subsystem, category: category)
        logger.log(level: level, "\(message, privacy: .public)")
        writeToFile(level: level, category: category, message: message)
    }

    /// Convenience: info level
    static func info(_ category: String, _ message: String) {
        log(.info, category: category, message)
    }

    /// Convenience: error level
    static func error(_ category: String, _ message: String) {
        log(.error, category: category, message)
    }

    /// Convenience: debug level
    static func debug(_ category: String, _ message: String) {
        log(.debug, category: category, message)
    }

    private static func writeToFile(level: OSLogType, category: String, message: String) {
        let timestamp = dateFormatter.string(from: Date())
        let levelStr: String
        switch level {
        case .error: levelStr = "ERROR"
        case .fault: levelStr = "FAULT"
        case .debug: levelStr = "DEBUG"
        case .info:  levelStr = "INFO"
        default:     levelStr = "LOG"
        }

        let line = "[\(timestamp)] [\(levelStr)] [\(category)] \(message)\n"

        fileLock.lock()
        defer { fileLock.unlock() }

        // Rotate if > 2 MB
        if let attrs = try? FileManager.default.attributesOfItem(atPath: logFile.path),
           let size = attrs[.size] as? UInt64, size > 2_000_000 {
            let backup = logDir.appendingPathComponent("app.log.1")
            try? FileManager.default.removeItem(at: backup)
            try? FileManager.default.moveItem(at: logFile, to: backup)
        }

        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: logFile.path) {
                if let handle = try? FileHandle(forWritingTo: logFile) {
                    handle.seekToEndOfFile()
                    handle.write(data)
                    handle.closeFile()
                }
            } else {
                try? data.write(to: logFile)
            }
        }
    }
}
