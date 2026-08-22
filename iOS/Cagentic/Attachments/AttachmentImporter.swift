@preconcurrency import ImageIO
@preconcurrency import PDFKit
import Foundation
import UniformTypeIdentifiers

nonisolated enum AttachmentImporter {
    private static let textFileExtensions: Set<String> = [
        "c", "cc", "conf", "cpp", "css", "csv", "go", "h", "hpp", "html", "java",
        "js", "json", "jsx", "kt", "log", "md", "mjs", "php", "plist", "properties",
        "py", "rb", "rs", "sh", "sql", "swift", "toml", "ts", "tsx", "txt", "xml",
        "yaml", "yml",
    ]

    static func prepare(
        source: AttachmentImportSource,
        limits: AttachmentImportLimits = .default
    ) async throws -> PreparedAttachment {
        let task = Task { @concurrent in
            try prepareSynchronously(source: source, limits: limits)
        }

        return try await withTaskCancellationHandler {
            try await task.value
        } onCancel: {
            task.cancel()
        }
    }

    static func preparePhoto(
        source: AttachmentPhotoDataSource,
        limits: AttachmentImportLimits = .default
    ) async throws -> PreparedAttachment {
        let task = Task { @concurrent in
            try Task.checkCancellation()
            guard source.data.count <= limits.maximumPhotoInputBytes else {
                throw AttachmentError.inputTooLarge(
                    fileName: sanitizedDisplayName(source.displayName),
                    maximumBytes: limits.maximumPhotoInputBytes
                )
            }
            return try preparedPhoto(
                sourceData: source.data,
                displayName: sanitizedDisplayName(source.displayName),
                declaredContentTypeIdentifier: source.declaredContentTypeIdentifier,
                limits: limits
            )
        }

        return try await withTaskCancellationHandler {
            try await task.value
        } onCancel: {
            task.cancel()
        }
    }

    private static func prepareSynchronously(
        source: AttachmentImportSource,
        limits: AttachmentImportLimits
    ) throws -> PreparedAttachment {
        try Task.checkCancellation()
        guard source.url.isFileURL else {
            throw AttachmentError.sourceMustBeFileURL
        }

        let didStartSecurityScope = source.url.startAccessingSecurityScopedResource()
        defer {
            if didStartSecurityScope {
                source.url.stopAccessingSecurityScopedResource()
            }
        }

        let displayName = sanitizedDisplayName(for: source.url)
        let type = resolvedType(for: source)

        if type?.conforms(to: .image) == true {
            return try preparePhoto(
                at: source.url,
                displayName: displayName,
                limits: limits
            )
        }

        if type?.conforms(to: .pdf) == true || source.url.pathExtension.lowercased() == "pdf" {
            return try preparePDF(
                at: source.url,
                displayName: displayName,
                limits: limits
            )
        }

        let pathExtension = source.url.pathExtension.lowercased()
        if type?.conforms(to: .text) == true || textFileExtensions.contains(pathExtension) {
            return try prepareText(
                at: source.url,
                displayName: displayName,
                sourceContentType: type?.preferredMIMEType ?? "text/plain",
                limits: limits
            )
        }

        throw AttachmentError.unsupportedFileType(fileName: displayName)
    }

    private static func preparePhoto(
        at url: URL,
        displayName: String,
        limits: AttachmentImportLimits
    ) throws -> PreparedAttachment {
        let sourceData = try boundedData(
            at: url,
            displayName: displayName,
            maximumBytes: limits.maximumPhotoInputBytes
        )
        return try preparedPhoto(
            sourceData: sourceData,
            displayName: displayName,
            declaredContentTypeIdentifier: nil,
            limits: limits
        )
    }

    private static func preparedPhoto(
        sourceData: Data,
        displayName: String,
        declaredContentTypeIdentifier: String?,
        limits: AttachmentImportLimits
    ) throws -> PreparedAttachment {
        try Task.checkCancellation()

        let sourceOptions = [kCGImageSourceShouldCache: false] as CFDictionary
        guard
            let imageSource = CGImageSourceCreateWithData(sourceData as CFData, sourceOptions),
            CGImageSourceGetCount(imageSource) > 0
        else {
            throw AttachmentError.invalidImage(fileName: displayName)
        }

        let decodedTypeIdentifier = CGImageSourceGetType(imageSource) as String?
        let sourceTypeIdentifier = decodedTypeIdentifier ?? declaredContentTypeIdentifier
        let sourceContentType = sourceTypeIdentifier
            .flatMap(UTType.init)
            .flatMap(\.preferredMIMEType) ?? "image/*"

        var candidateDimensions = [limits.maximumPhotoPixelDimension, 1_536, 1_024, 768]
            .filter { $0 > 0 && $0 <= limits.maximumPhotoPixelDimension }
        candidateDimensions = Array(Set(candidateDimensions)).sorted(by: >)

        for maximumDimension in candidateDimensions {
            try Task.checkCancellation()
            let thumbnailOptions = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: maximumDimension,
            ] as CFDictionary
            guard let image = CGImageSourceCreateThumbnailAtIndex(imageSource, 0, thumbnailOptions)
            else {
                throw AttachmentError.invalidImage(fileName: displayName)
            }

            for quality in [0.82, 0.68, 0.52] {
                try Task.checkCancellation()
                guard let output = encodedJPEG(image, quality: quality) else {
                    throw AttachmentError.invalidImage(fileName: displayName)
                }
                if output.count <= limits.maximumProcessedPhotoBytes {
                    return PreparedAttachment(
                        kind: .photo,
                        displayName: displayName,
                        sourceContentType: sourceContentType,
                        payloadContentType: "image/jpeg",
                        payloadFormat: .jpeg,
                        payload: output,
                        pixelWidth: image.width,
                        pixelHeight: image.height,
                        extractedCharacterCount: nil
                    )
                }
            }
        }

        throw AttachmentError.processedImageTooLarge(
            maximumBytes: limits.maximumProcessedPhotoBytes
        )
    }

    private static func encodedJPEG(_ image: CGImage, quality: Double) -> Data? {
        let data = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                data,
                UTType.jpeg.identifier as CFString,
                1,
                nil
            )
        else {
            return nil
        }

        // Supplying only a decoded CGImage and compression quality intentionally omits EXIF,
        // location, camera, and other source metadata from the new attachment payload.
        let properties = [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary
        CGImageDestinationAddImage(destination, image, properties)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return data as Data
    }

    private static func prepareText(
        at url: URL,
        displayName: String,
        sourceContentType: String,
        limits: AttachmentImportLimits
    ) throws -> PreparedAttachment {
        let data = try boundedData(
            at: url,
            displayName: displayName,
            maximumBytes: limits.maximumTextInputBytes
        )
        let text = try decodedText(data, displayName: displayName)
        let normalized = normalizedText(text)
        let characterCount = normalized.unicodeScalars.count
        guard characterCount <= limits.maximumTextCharacters else {
            throw AttachmentError.textTooLong(
                fileName: displayName,
                maximumCharacters: limits.maximumTextCharacters
            )
        }
        guard let payload = normalized.data(using: .utf8) else {
            throw AttachmentError.unsupportedTextEncoding(fileName: displayName)
        }

        return PreparedAttachment(
            kind: .textFile,
            displayName: displayName,
            sourceContentType: sourceContentType,
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            payload: payload,
            pixelWidth: nil,
            pixelHeight: nil,
            extractedCharacterCount: characterCount
        )
    }

    private static func preparePDF(
        at url: URL,
        displayName: String,
        limits: AttachmentImportLimits
    ) throws -> PreparedAttachment {
        let data = try boundedData(
            at: url,
            displayName: displayName,
            maximumBytes: limits.maximumPDFInputBytes
        )
        try Task.checkCancellation()

        guard let document = PDFDocument(data: data) else {
            throw AttachmentError.pdfHasNoExtractableText(fileName: displayName)
        }
        guard !document.isLocked else {
            throw AttachmentError.encryptedPDF(fileName: displayName)
        }

        var pages: [String] = []
        var characterCount = 0
        pages.reserveCapacity(min(document.pageCount, 128))

        for index in 0..<document.pageCount {
            try Task.checkCancellation()
            guard let pageText = document.page(at: index)?.string else { continue }
            let normalizedPage = normalizedText(pageText)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalizedPage.isEmpty else { continue }

            characterCount += normalizedPage.unicodeScalars.count
            if !pages.isEmpty {
                characterCount += 2
            }
            guard characterCount <= limits.maximumPDFCharacters else {
                throw AttachmentError.textTooLong(
                    fileName: displayName,
                    maximumCharacters: limits.maximumPDFCharacters
                )
            }
            pages.append(normalizedPage)
        }

        let extractedText = pages.joined(separator: "\n\n")
        guard !extractedText.isEmpty else {
            throw AttachmentError.pdfHasNoExtractableText(fileName: displayName)
        }
        guard let payload = extractedText.data(using: .utf8) else {
            throw AttachmentError.pdfHasNoExtractableText(fileName: displayName)
        }

        return PreparedAttachment(
            kind: .pdf,
            displayName: displayName,
            sourceContentType: "application/pdf",
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            payload: payload,
            pixelWidth: nil,
            pixelHeight: nil,
            extractedCharacterCount: characterCount
        )
    }

    private static func boundedData(
        at url: URL,
        displayName: String,
        maximumBytes: Int
    ) throws -> Data {
        do {
            if let fileSize = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
               fileSize > maximumBytes
            {
                throw AttachmentError.inputTooLarge(
                    fileName: displayName,
                    maximumBytes: maximumBytes
                )
            }

            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }

            var result = Data()
            result.reserveCapacity(min(maximumBytes, 256 * 1_024))
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
                throw AttachmentError.inputTooLarge(
                    fileName: displayName,
                    maximumBytes: maximumBytes
                )
            }
            return result
        } catch let error as AttachmentError {
            throw error
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw AttachmentError.cannotRead(fileName: displayName)
        }
    }

    private static func decodedText(_ data: Data, displayName: String) throws -> String {
        let text: String?
        if data.starts(with: [0xEF, 0xBB, 0xBF]) {
            text = String(data: data.dropFirst(3), encoding: .utf8)
        } else if data.starts(with: [0xFF, 0xFE]) {
            text = String(data: data.dropFirst(2), encoding: .utf16LittleEndian)
        } else if data.starts(with: [0xFE, 0xFF]) {
            text = String(data: data.dropFirst(2), encoding: .utf16BigEndian)
        } else {
            text = String(data: data, encoding: .utf8)
        }

        guard let text, !text.unicodeScalars.contains(where: { $0.value == 0 }) else {
            throw AttachmentError.unsupportedTextEncoding(fileName: displayName)
        }
        return text
    }

    private static func normalizedText(_ text: String) -> String {
        text.replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
    }

    private static func resolvedType(for source: AttachmentImportSource) -> UTType? {
        if let identifier = source.declaredContentTypeIdentifier,
           let declaredType = UTType(identifier)
        {
            return declaredType
        }

        if let resourceType = try? source.url.resourceValues(forKeys: [.contentTypeKey]).contentType {
            return resourceType
        }

        let pathExtension = source.url.pathExtension
        return pathExtension.isEmpty ? nil : UTType(filenameExtension: pathExtension)
    }

    private static func sanitizedDisplayName(for url: URL) -> String {
        sanitizedDisplayName(url.lastPathComponent)
    }

    private static func sanitizedDisplayName(_ proposedName: String) -> String {
        let pathSafeName = proposedName
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: "\\", with: "-")
        let scalars = pathSafeName.precomposedStringWithCanonicalMapping.unicodeScalars
            .filter { !CharacterSet.controlCharacters.contains($0) }
        let cleaned = String(String.UnicodeScalarView(scalars))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else { return "Attachment" }
        return String(cleaned.prefix(120))
    }
}
