@preconcurrency import ImageIO
import CoreText
import Foundation
import Testing
import UniformTypeIdentifiers
@testable import Cagentic

struct AttachmentStoreTests {
    @Test("Attachment metadata has a stable Codable round trip without binary payloads")
    func metadataRoundTrip() throws {
        let id = UUID(uuidString: "B0A41D86-A24E-4D55-AF7D-F408465C05F1")!
        let metadata = AttachmentMetadata(
            id: id,
            kind: .photo,
            displayName: "desk.jpg",
            sourceContentType: "image/jpeg",
            payloadContentType: "image/jpeg",
            payloadFormat: .jpeg,
            byteCount: 123,
            createdAt: Date(timeIntervalSinceReferenceDate: 42),
            storageKey: "\(id.uuidString.lowercased()).jpg",
            pixelWidth: 100,
            pixelHeight: 80
        )

        let data = try JSONEncoder().encode(metadata)
        let decoded = try JSONDecoder().decode(AttachmentMetadata.self, from: data)
        let json = String(decoding: data, as: UTF8.self)

        #expect(decoded == metadata)
        #expect(!json.contains("base64"))
        #expect(!json.contains("payload" + "Data"))
    }

    @Test("UTF-8 text is normalized, bounded, and stored separately")
    func textImportRoundTrip() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.swift")
        try Data("let answer = 42\r\nprint(answer)\r".utf8).write(to: fixture.fileURL)
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)

        let metadata = try await store.importAttachment(
            from: AttachmentImportSource(url: fixture.fileURL)
        )
        let text = try await store.extractedText(for: metadata)

        #expect(metadata.kind == .textFile)
        #expect(metadata.payloadFormat == .utf8Text)
        #expect(text == "let answer = 42\nprint(answer)\n")
        #expect(metadata.byteCount == Int64(text.utf8.count))
    }

    @Test("Startup reconciliation removes orphaned payloads and keeps referenced files")
    func startupReconciliation() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.txt")
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)
        try Data("keep".utf8).write(to: fixture.fileURL)
        let referenced = try await store.importAttachment(
            from: AttachmentImportSource(url: fixture.fileURL)
        )
        try Data("orphan".utf8).write(to: fixture.fileURL)
        let orphaned = try await store.importAttachment(
            from: AttachmentImportSource(url: fixture.fileURL)
        )

        try await store.reconcileStorage(referencedAttachments: [referenced])

        #expect(try await store.extractedText(for: referenced) == "keep")
        await #expect(throws: AttachmentError.attachmentNotFound(displayName: "notes.txt")) {
            _ = try await store.payloadData(for: orphaned)
        }
    }

    @Test("Oversized source files fail before they are persisted")
    func oversizedTextIsRejected() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "large.txt")
        try Data(repeating: 0x41, count: 9).write(to: fixture.fileURL)
        var limits = AttachmentImportLimits.default
        limits.maximumTextInputBytes = 8
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory, limits: limits)

        await #expect(throws: AttachmentError.inputTooLarge(
            fileName: "large.txt",
            maximumBytes: 8
        )) {
            try await store.importAttachment(from: AttachmentImportSource(url: fixture.fileURL))
        }
    }

    @Test("Photos are downsampled, stripped, and base64 encoded only on request")
    func photoImportProducesOllamaPayload() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "photo.png")
        try makePNG(width: 320, height: 160, includesGPSMetadata: true).write(to: fixture.fileURL)
        var limits = AttachmentImportLimits.default
        limits.maximumPhotoPixelDimension = 64
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory, limits: limits)

        let metadata = try await store.importAttachment(
            from: AttachmentImportSource(
                url: fixture.fileURL,
                declaredContentTypeIdentifier: UTType.png.identifier
            )
        )
        let payload = try await store.payloadData(for: metadata)
        let base64 = try await store.ollamaImageBase64(for: metadata)

        #expect(metadata.kind == .photo)
        #expect(metadata.payloadFormat == .jpeg)
        #expect(max(metadata.pixelWidth ?? .max, metadata.pixelHeight ?? .max) <= 64)
        #expect(Data(base64Encoded: base64) == payload)

        let source = try #require(CGImageSourceCreateWithData(payload as CFData, nil))
        let properties = try #require(
            CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
        )
        #expect(properties[kCGImagePropertyGPSDictionary] == nil)
    }

    @Test("PhotosPicker data uses the same bounded off-main image pipeline")
    func photoDataImport() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "unused")
        let png = try makePNG(width: 40, height: 20, includesGPSMetadata: false)
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)

        let metadata = try await store.importPhoto(
            from: AttachmentPhotoDataSource(
                data: png,
                displayName: "Library photo.png",
                declaredContentTypeIdentifier: UTType.png.identifier
            )
        )

        #expect(metadata.kind == .photo)
        #expect(metadata.displayName == "Library photo.png")
        #expect(metadata.sourceContentType == "image/png")
    }

    @Test("Searchable PDFs are converted to bounded UTF-8 context")
    func searchablePDFImport() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "reference.pdf")
        try makePDF(text: "Searchable attachment text").write(to: fixture.fileURL)
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)

        let metadata = try await store.importAttachment(
            from: AttachmentImportSource(
                url: fixture.fileURL,
                declaredContentTypeIdentifier: UTType.pdf.identifier
            )
        )
        let text = try await store.extractedText(for: metadata)

        #expect(metadata.kind == .pdf)
        #expect(metadata.payloadFormat == .utf8Text)
        #expect(text.contains("Searchable attachment text"))
    }

    @Test("Forged storage paths are rejected")
    func pathTraversalIsRejected() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.txt")
        let id = UUID()
        let metadata = AttachmentMetadata(
            id: id,
            kind: .textFile,
            displayName: "notes.txt",
            sourceContentType: "text/plain",
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            byteCount: 1,
            storageKey: "../\(id.uuidString.lowercased()).txt",
            extractedCharacterCount: 1
        )
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)

        await #expect(throws: AttachmentError.invalidMetadata) {
            try await store.payloadData(for: metadata)
        }
    }

    @Test("A cancelled import does not commit an attachment")
    func cancellationStopsImport() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.txt")
        try Data(repeating: 0x41, count: 256 * 1_024).write(to: fixture.fileURL)
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory)

        let task = Task {
            await Task.yield()
            return try await store.importAttachment(
                from: AttachmentImportSource(url: fixture.fileURL)
            )
        }
        task.cancel()

        await #expect(throws: CancellationError.self) {
            try await task.value
        }
    }

    @Test("Selection validation applies count and aggregate byte limits")
    func selectionLimits() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.txt")
        var limits = AttachmentImportLimits.default
        limits.maximumAttachmentCount = 1
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory, limits: limits)
        let id = UUID()
        let metadata = AttachmentMetadata(
            id: id,
            kind: .textFile,
            displayName: "notes.txt",
            sourceContentType: "text/plain",
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            byteCount: 1,
            storageKey: "\(id.uuidString.lowercased()).txt",
            extractedCharacterCount: 1
        )

        await #expect(throws: AttachmentError.tooManyAttachments(maximumCount: 1)) {
            try await store.validateSelection([metadata, metadata])
        }
    }

    @Test("Selection validation rejects an oversized aggregate payload")
    func aggregateSelectionLimit() async throws {
        let fixture = try TemporaryAttachmentFixture(fileName: "notes.txt")
        var limits = AttachmentImportLimits.default
        limits.maximumBatchPayloadBytes = 1
        let store = AttachmentStore(rootDirectory: fixture.storageDirectory, limits: limits)
        let id = UUID()
        let metadata = AttachmentMetadata(
            id: id,
            kind: .textFile,
            displayName: "notes.txt",
            sourceContentType: "text/plain",
            payloadContentType: "text/plain; charset=utf-8",
            payloadFormat: .utf8Text,
            byteCount: 2,
            storageKey: "\(id.uuidString.lowercased()).txt",
            extractedCharacterCount: 2
        )

        await #expect(throws: AttachmentError.totalPayloadTooLarge(maximumBytes: 1)) {
            try await store.validateSelection([metadata])
        }
    }
}

private struct TemporaryAttachmentFixture {
    let directory: URL
    let fileURL: URL
    let storageDirectory: URL

    init(fileName: String) throws {
        directory = FileManager.default.temporaryDirectory
            .appending(path: UUID().uuidString, directoryHint: .isDirectory)
        fileURL = directory.appending(path: fileName, directoryHint: .notDirectory)
        storageDirectory = directory.appending(path: "stored", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }
}

private func makePNG(width: Int, height: Int, includesGPSMetadata: Bool) throws -> Data {
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    let context = try #require(
        CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    )
    context.setFillColor(red: 0.42, green: 0.24, blue: 0.55, alpha: 1)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    let image = try #require(context.makeImage())

    let data = NSMutableData()
    let destination = try #require(
        CGImageDestinationCreateWithData(
            data,
            UTType.png.identifier as CFString,
            1,
            nil
        )
    )
    var properties: [CFString: Any] = [:]
    if includesGPSMetadata {
        properties[kCGImagePropertyGPSDictionary] = [kCGImagePropertyGPSLatitude: 37.0]
    }
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    #expect(CGImageDestinationFinalize(destination))
    return data as Data
}

private func makePDF(text: String) throws -> Data {
    let data = NSMutableData()
    let consumer = try #require(CGDataConsumer(data: data as CFMutableData))
    var mediaBox = CGRect(x: 0, y: 0, width: 300, height: 200)
    let context = try #require(CGContext(consumer: consumer, mediaBox: &mediaBox, nil))
    context.beginPDFPage(nil)
    context.textPosition = CGPoint(x: 24, y: 100)
    let font = CTFontCreateWithName("Helvetica" as CFString, 14, nil)
    let attributes = [kCTFontAttributeName: font] as CFDictionary
    let attributed = try #require(CFAttributedStringCreate(nil, text as CFString, attributes))
    CTLineDraw(CTLineCreateWithAttributedString(attributed), context)
    context.endPDFPage()
    context.closePDF()
    return data as Data
}
