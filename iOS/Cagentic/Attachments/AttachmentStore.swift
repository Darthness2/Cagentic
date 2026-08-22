import Foundation

actor AttachmentStore {
    private let rootDirectory: URL
    private let limits: AttachmentImportLimits

    init(rootDirectory: URL, limits: AttachmentImportLimits = .default) {
        self.rootDirectory = rootDirectory.standardizedFileURL
        self.limits = limits
    }

    static func live(
        fileManager: FileManager = .default,
        limits: AttachmentImportLimits = .default
    ) -> AttachmentStore {
        let baseDirectory = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? fileManager.temporaryDirectory
        let directory = baseDirectory
            .appending(path: "Cagentic", directoryHint: .isDirectory)
            .appending(path: "Attachments", directoryHint: .isDirectory)
        return AttachmentStore(rootDirectory: directory, limits: limits)
    }

    func importAttachment(from source: AttachmentImportSource) async throws -> AttachmentMetadata {
        let prepared = try await AttachmentImporter.prepare(source: source, limits: limits)
        try validatePreparedPayload(prepared)
        return try store(prepared)
    }

    func importPhoto(from source: AttachmentPhotoDataSource) async throws -> AttachmentMetadata {
        let prepared = try await AttachmentImporter.preparePhoto(source: source, limits: limits)
        try validatePreparedPayload(prepared)
        return try store(prepared)
    }

    /// Processes all selected items before committing any file so selection-limit failures do not
    /// leave partial records behind.
    func importAttachments(
        from sources: [AttachmentImportSource]
    ) async throws -> [AttachmentMetadata] {
        guard sources.count <= limits.maximumAttachmentCount else {
            throw AttachmentError.tooManyAttachments(
                maximumCount: limits.maximumAttachmentCount
            )
        }

        var preparedItems: [PreparedAttachment] = []
        preparedItems.reserveCapacity(sources.count)
        var totalBytes = 0
        for source in sources {
            try Task.checkCancellation()
            let prepared = try await AttachmentImporter.prepare(source: source, limits: limits)
            try validatePreparedPayload(prepared)
            totalBytes = try addingPayloadBytes(totalBytes, prepared.payload.count)
            preparedItems.append(prepared)
        }

        guard totalBytes <= limits.maximumBatchPayloadBytes else {
            throw AttachmentError.totalPayloadTooLarge(
                maximumBytes: limits.maximumBatchPayloadBytes
            )
        }

        var stored: [AttachmentMetadata] = []
        stored.reserveCapacity(preparedItems.count)
        do {
            for prepared in preparedItems {
                try Task.checkCancellation()
                stored.append(try store(prepared))
            }
            return stored
        } catch {
            for metadata in stored {
                if let url = try? validatedURL(for: metadata) {
                    try? FileManager.default.removeItem(at: url)
                }
            }
            throw error
        }
    }

    func validateSelection(_ attachments: [AttachmentMetadata]) throws {
        guard attachments.count <= limits.maximumAttachmentCount else {
            throw AttachmentError.tooManyAttachments(
                maximumCount: limits.maximumAttachmentCount
            )
        }

        var totalBytes = 0
        for attachment in attachments {
            try validate(metadata: attachment)
            guard attachment.byteCount >= 0, attachment.byteCount <= Int64(Int.max) else {
                throw AttachmentError.invalidMetadata
            }
            totalBytes = try addingPayloadBytes(totalBytes, Int(attachment.byteCount))
        }
        guard totalBytes <= limits.maximumBatchPayloadBytes else {
            throw AttachmentError.totalPayloadTooLarge(
                maximumBytes: limits.maximumBatchPayloadBytes
            )
        }
    }

    func requestContextPlan(
        for messages: [ChatMessage],
        systemPrompt: String,
        maximumEstimatedTokens: Int
    ) throws -> OllamaRequestContextPlan {
        try OllamaRequestContextPlanner.plan(
            messages: messages,
            systemPrompt: systemPrompt,
            maximumEncodedBytes: limits.maximumRequestContextBytes,
            maximumEstimatedTokens: maximumEstimatedTokens
        )
    }

    func payloadData(for attachment: AttachmentMetadata) throws -> Data {
        let url = try validatedURL(for: attachment)
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw AttachmentError.attachmentNotFound(displayName: attachment.displayName)
        }

        do {
            let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true else {
                throw AttachmentError.invalidMetadata
            }
        } catch let error as AttachmentError {
            throw error
        } catch {
            throw AttachmentError.cannotRead(fileName: attachment.displayName)
        }

        let data: Data
        do {
            data = try readBoundedPayload(at: url)
        } catch let error as AttachmentError {
            throw error
        } catch {
            throw AttachmentError.cannotRead(fileName: attachment.displayName)
        }
        guard data.count == attachment.byteCount else {
            throw AttachmentError.invalidMetadata
        }
        return data
    }

    func ollamaImageBase64(for attachment: AttachmentMetadata) throws -> String {
        guard attachment.isOllamaImage else {
            throw AttachmentError.payloadTypeMismatch
        }
        // Encoding is intentionally delayed until the request is being assembled.
        return try payloadData(for: attachment).base64EncodedString()
    }

    func ollamaImageBase64(for attachments: [AttachmentMetadata]) throws -> [String] {
        try validateSelection(attachments)
        return try attachments.filter(\.isOllamaImage).map(ollamaImageBase64)
    }

    func extractedText(for attachment: AttachmentMetadata) throws -> String {
        guard attachment.payloadFormat == .utf8Text else {
            throw AttachmentError.payloadTypeMismatch
        }
        let data = try payloadData(for: attachment)
        guard let text = String(data: data, encoding: .utf8) else {
            throw AttachmentError.invalidMetadata
        }
        return text
    }

    func remove(_ attachment: AttachmentMetadata) throws {
        let url = try validatedURL(for: attachment)
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        do {
            try FileManager.default.removeItem(at: url)
        } catch {
            throw AttachmentError.cannotStore(displayName: attachment.displayName)
        }
    }

    /// Removes payloads that are not referenced by durable messages or persisted drafts. This also
    /// recovers files left behind by a terminated import or cleanup task.
    func reconcileStorage(referencedAttachments: [AttachmentMetadata]) throws {
        let fileManager = FileManager.default
        guard fileManager.fileExists(atPath: rootDirectory.path) else { return }

        let referencedKeys = Set(referencedAttachments.compactMap { attachment in
            (try? validatedURL(for: attachment)) == nil ? nil : attachment.storageKey
        })
        let storedURLs = try fileManager.contentsOfDirectory(
            at: rootDirectory,
            includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey],
            options: [.skipsHiddenFiles]
        )
        for url in storedURLs where !referencedKeys.contains(url.lastPathComponent) {
            try Task.checkCancellation()
            do {
                try fileManager.removeItem(at: url)
            } catch {
                throw AttachmentError.cannotStore(displayName: url.lastPathComponent)
            }
        }
    }

    private func store(_ prepared: PreparedAttachment) throws -> AttachmentMetadata {
        let id = UUID()
        let storageKey = "\(id.uuidString.lowercased()).\(prepared.payloadFormat.fileExtension)"
        let metadata = AttachmentMetadata(
            id: id,
            kind: prepared.kind,
            displayName: prepared.displayName,
            sourceContentType: prepared.sourceContentType,
            payloadContentType: prepared.payloadContentType,
            payloadFormat: prepared.payloadFormat,
            byteCount: Int64(prepared.payload.count),
            storageKey: storageKey,
            pixelWidth: prepared.pixelWidth,
            pixelHeight: prepared.pixelHeight,
            extractedCharacterCount: prepared.extractedCharacterCount
        )

        do {
            try FileManager.default.createDirectory(
                at: rootDirectory,
                withIntermediateDirectories: true,
                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication]
            )
            try Task.checkCancellation()
            let url = try validatedURL(for: metadata)
            try prepared.payload.write(to: url, options: [.atomic, .completeFileProtection])
            do {
                try Task.checkCancellation()
            } catch {
                try? FileManager.default.removeItem(at: url)
                throw error
            }
            return metadata
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as AttachmentError {
            throw error
        } catch {
            throw AttachmentError.cannotStore(displayName: prepared.displayName)
        }
    }

    private func validatePreparedPayload(_ prepared: PreparedAttachment) throws {
        let maximumBytes: Int
        switch prepared.payloadFormat {
        case .jpeg:
            maximumBytes = limits.maximumProcessedPhotoBytes
        case .utf8Text:
            maximumBytes = max(limits.maximumTextCharacters, limits.maximumPDFCharacters) * 4
        }
        guard prepared.payload.count <= maximumBytes else {
            throw AttachmentError.totalPayloadTooLarge(maximumBytes: maximumBytes)
        }
        guard prepared.payload.count <= limits.maximumBatchPayloadBytes else {
            throw AttachmentError.totalPayloadTooLarge(
                maximumBytes: limits.maximumBatchPayloadBytes
            )
        }
    }

    private func validate(metadata: AttachmentMetadata) throws {
        guard
            metadata.schemaVersion == AttachmentMetadata.currentSchemaVersion,
            metadata.byteCount >= 0,
            metadata.byteCount <= Int64(limits.maximumStoredPayloadBytes),
            !metadata.displayName.isEmpty,
            metadata.displayName.count <= 120,
            !metadata.sourceContentType.isEmpty,
            !metadata.payloadContentType.isEmpty
        else {
            throw AttachmentError.invalidMetadata
        }

        switch (metadata.kind, metadata.payloadFormat) {
        case (.photo, .jpeg):
            guard
                metadata.payloadContentType == "image/jpeg",
                let pixelWidth = metadata.pixelWidth,
                let pixelHeight = metadata.pixelHeight,
                pixelWidth > 0,
                pixelHeight > 0,
                metadata.extractedCharacterCount == nil
            else {
                throw AttachmentError.invalidMetadata
            }
        case (.textFile, .utf8Text), (.pdf, .utf8Text):
            guard
                metadata.payloadContentType.hasPrefix("text/plain"),
                let characterCount = metadata.extractedCharacterCount,
                characterCount >= 0,
                metadata.pixelWidth == nil,
                metadata.pixelHeight == nil
            else {
                throw AttachmentError.invalidMetadata
            }
        default:
            throw AttachmentError.invalidMetadata
        }
    }

    private func validatedURL(for metadata: AttachmentMetadata) throws -> URL {
        try validate(metadata: metadata)
        let expectedKey = "\(metadata.id.uuidString.lowercased()).\(metadata.payloadFormat.fileExtension)"
        guard metadata.storageKey == expectedKey,
              metadata.storageKey == URL(fileURLWithPath: metadata.storageKey).lastPathComponent
        else {
            throw AttachmentError.invalidMetadata
        }

        let url = rootDirectory
            .appending(path: metadata.storageKey, directoryHint: .notDirectory)
            .standardizedFileURL
        guard url.deletingLastPathComponent() == rootDirectory else {
            throw AttachmentError.invalidMetadata
        }
        return url
    }

    private func readBoundedPayload(at url: URL) throws -> Data {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }

        var result = Data()
        let maximumBytes = limits.maximumStoredPayloadBytes
        while result.count <= maximumBytes {
            try Task.checkCancellation()
            let remaining = maximumBytes + 1 - result.count
            guard let chunk = try handle.read(upToCount: min(64 * 1_024, remaining)),
                  !chunk.isEmpty
            else {
                break
            }
            result.append(chunk)
        }
        guard result.count <= maximumBytes else {
            throw AttachmentError.invalidMetadata
        }
        return result
    }

    private func addingPayloadBytes(_ total: Int, _ next: Int) throws -> Int {
        let (sum, overflow) = total.addingReportingOverflow(next)
        guard !overflow else {
            throw AttachmentError.totalPayloadTooLarge(
                maximumBytes: limits.maximumBatchPayloadBytes
            )
        }
        return sum
    }
}
