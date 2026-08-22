import Foundation

protocol AppPersisting: Sendable {
    func load() async throws -> AppSnapshot?
    func save(_ snapshot: AppSnapshot, revision: Int) async throws
}

extension AppPersisting {
    func save(_ snapshot: AppSnapshot) async throws {
        try await save(snapshot, revision: 0)
    }
}

actor JSONAppRepository: AppPersisting {
    private let fileURL: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private var latestSuccessfulRevision = Int.min

    init(fileURL: URL) {
        self.fileURL = fileURL

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(date.timeIntervalSinceReferenceDate.bitPattern)
        }
        self.encoder = encoder

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let bitPattern = try container.decode(UInt64.self)
            return Date(timeIntervalSinceReferenceDate: Double(bitPattern: bitPattern))
        }
        self.decoder = decoder
    }

    static func live(fileManager: FileManager = .default) -> JSONAppRepository {
        let baseDirectory = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.temporaryDirectory
        let directory = baseDirectory.appending(path: "Cagentic", directoryHint: .isDirectory)
        return JSONAppRepository(
            fileURL: directory.appending(path: "app-state.json", directoryHint: .notDirectory)
        )
    }

    func load() async throws -> AppSnapshot? {
        let fileManager = FileManager.default
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return nil
        }

        let data = try Data(contentsOf: fileURL)
        return try decoder.decode(AppSnapshot.self, from: data)
    }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        try Task.checkCancellation()
        guard revision > latestSuccessfulRevision else { return }
        let fileManager = FileManager.default
        let directory = fileURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try Task.checkCancellation()
        let data = try encoder.encode(snapshot)
        try Task.checkCancellation()
        try data.write(to: fileURL, options: [.atomic, .completeFileProtection])
        latestSuccessfulRevision = revision
    }
}

actor InMemoryAppRepository: AppPersisting {
    private var snapshot: AppSnapshot?
    private var latestSuccessfulRevision = Int.min

    init(snapshot: AppSnapshot? = nil) {
        self.snapshot = snapshot
    }

    func load() async throws -> AppSnapshot? {
        snapshot
    }

    func save(_ snapshot: AppSnapshot, revision: Int) async throws {
        try Task.checkCancellation()
        guard revision > latestSuccessfulRevision else { return }
        self.snapshot = snapshot
        latestSuccessfulRevision = revision
    }
}
