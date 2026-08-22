import Foundation

nonisolated enum AttachmentKind: String, Codable, CaseIterable, Hashable, Sendable {
    case photo
    case textFile
    case pdf
}

nonisolated enum AttachmentPayloadFormat: String, Codable, Hashable, Sendable {
    case jpeg
    case utf8Text

    var fileExtension: String {
        switch self {
        case .jpeg: "jpg"
        case .utf8Text: "txt"
        }
    }
}

/// Persistable attachment metadata. Binary payloads live in `AttachmentStore` and are never
/// embedded in conversation JSON. In particular, image base64 is produced only at send time.
nonisolated struct AttachmentMetadata: Identifiable, Codable, Equatable, Hashable, Sendable {
    static let currentSchemaVersion = 1

    let schemaVersion: Int
    let id: UUID
    let kind: AttachmentKind
    let displayName: String
    let sourceContentType: String
    let payloadContentType: String
    let payloadFormat: AttachmentPayloadFormat
    let byteCount: Int64
    let createdAt: Date
    let storageKey: String
    let pixelWidth: Int?
    let pixelHeight: Int?
    let extractedCharacterCount: Int?

    init(
        schemaVersion: Int = AttachmentMetadata.currentSchemaVersion,
        id: UUID = UUID(),
        kind: AttachmentKind,
        displayName: String,
        sourceContentType: String,
        payloadContentType: String,
        payloadFormat: AttachmentPayloadFormat,
        byteCount: Int64,
        createdAt: Date = .now,
        storageKey: String,
        pixelWidth: Int? = nil,
        pixelHeight: Int? = nil,
        extractedCharacterCount: Int? = nil
    ) {
        self.schemaVersion = schemaVersion
        self.id = id
        self.kind = kind
        self.displayName = displayName
        self.sourceContentType = sourceContentType
        self.payloadContentType = payloadContentType
        self.payloadFormat = payloadFormat
        self.byteCount = byteCount
        self.createdAt = createdAt
        self.storageKey = storageKey
        self.pixelWidth = pixelWidth
        self.pixelHeight = pixelHeight
        self.extractedCharacterCount = extractedCharacterCount
    }

    var isOllamaImage: Bool {
        kind == .photo && payloadFormat == .jpeg
    }
}

nonisolated struct AttachmentImportSource: Equatable, Hashable, Sendable {
    let url: URL
    let declaredContentTypeIdentifier: String?

    init(url: URL, declaredContentTypeIdentifier: String? = nil) {
        self.url = url
        self.declaredContentTypeIdentifier = declaredContentTypeIdentifier
    }
}

/// A PhotosPicker-friendly source. `PhotosPickerItem.loadTransferable(type: Data.self)` can be
/// passed here without decoding through UIKit; the importer immediately enforces its byte limit
/// and performs ImageIO work away from the main actor.
nonisolated struct AttachmentPhotoDataSource: Equatable, Sendable {
    let data: Data
    let displayName: String
    let declaredContentTypeIdentifier: String?

    init(
        data: Data,
        displayName: String = "Photo",
        declaredContentTypeIdentifier: String? = nil
    ) {
        self.data = data
        self.displayName = displayName
        self.declaredContentTypeIdentifier = declaredContentTypeIdentifier
    }
}

nonisolated struct AttachmentImportLimits: Equatable, Sendable {
    var maximumAttachmentCount = 6
    var maximumBatchPayloadBytes = 16 * 1_024 * 1_024
    var maximumRequestContextBytes = 24 * 1_024 * 1_024
    var maximumPhotoInputBytes = 30 * 1_024 * 1_024
    var maximumProcessedPhotoBytes = 8 * 1_024 * 1_024
    var maximumPhotoPixelDimension = 2_048
    var maximumTextInputBytes = 2 * 1_024 * 1_024
    var maximumTextCharacters = 200_000
    var maximumPDFInputBytes = 20 * 1_024 * 1_024
    var maximumPDFCharacters = 200_000

    static let `default` = AttachmentImportLimits()

    var maximumStoredPayloadBytes: Int {
        max(maximumProcessedPhotoBytes, maximumTextCharacters * 4, maximumPDFCharacters * 4)
    }
}

nonisolated enum AttachmentError: Error, Equatable, LocalizedError, Sendable {
    case sourceMustBeFileURL
    case unsupportedFileType(fileName: String)
    case cannotRead(fileName: String)
    case inputTooLarge(fileName: String, maximumBytes: Int)
    case invalidImage(fileName: String)
    case processedImageTooLarge(maximumBytes: Int)
    case unsupportedTextEncoding(fileName: String)
    case textTooLong(fileName: String, maximumCharacters: Int)
    case encryptedPDF(fileName: String)
    case pdfHasNoExtractableText(fileName: String)
    case tooManyAttachments(maximumCount: Int)
    case totalPayloadTooLarge(maximumBytes: Int)
    case requestContextTooLarge(maximumBytes: Int)
    case attachmentNotFound(displayName: String)
    case invalidMetadata
    case payloadTypeMismatch
    case visionModelRequired
    case cannotStore(displayName: String)

    var errorDescription: String? {
        switch self {
        case .sourceMustBeFileURL:
            "Choose a file stored on this device or in a connected Files location."
        case .unsupportedFileType(let fileName):
            "\(fileName) is not a supported photo, text, code, or PDF file."
        case .cannotRead(let fileName):
            "Cagentic could not read \(fileName). Try downloading it in Files first."
        case .inputTooLarge(let fileName, let maximumBytes):
            "\(fileName) is too large. Choose a file under \(Self.size(maximumBytes))."
        case .invalidImage(let fileName):
            "\(fileName) could not be decoded as an image. Try exporting it as JPEG or PNG."
        case .processedImageTooLarge(let maximumBytes):
            "The optimized photo is still over \(Self.size(maximumBytes)). Choose a smaller photo."
        case .unsupportedTextEncoding(let fileName):
            "\(fileName) is not UTF-8 or UTF-16 text. Save it with a Unicode encoding and try again."
        case .textTooLong(let fileName, let maximumCharacters):
            "\(fileName) contains more than \(maximumCharacters.formatted()) characters."
        case .encryptedPDF(let fileName):
            "\(fileName) is password protected. Save an unlocked copy before attaching it."
        case .pdfHasNoExtractableText(let fileName):
            "No selectable text was found in \(fileName). Scanned PDFs need OCR before import."
        case .tooManyAttachments(let maximumCount):
            "Attach up to \(maximumCount) items to one message."
        case .totalPayloadTooLarge(let maximumBytes):
            "These attachments exceed the \(Self.size(maximumBytes)) total limit."
        case .requestContextTooLarge(let maximumBytes):
            "This turn cannot fit within Cagentic’s \(Self.size(maximumBytes)) request limit. Start a new chat or send fewer attachments."
        case .attachmentNotFound(let displayName):
            "\(displayName) is no longer available. Remove it and attach the file again."
        case .invalidMetadata:
            "This attachment record is invalid. Remove it and attach the file again."
        case .payloadTypeMismatch:
            "This attachment cannot be used in that part of the message."
        case .visionModelRequired:
            "The selected model does not accept images. Choose a vision model and try again."
        case .cannotStore(let displayName):
            "Cagentic could not securely save \(displayName). Check available device storage."
        }
    }

    private static func size(_ byteCount: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(byteCount), countStyle: .file)
    }
}

nonisolated struct PreparedAttachment: Equatable, Sendable {
    let kind: AttachmentKind
    let displayName: String
    let sourceContentType: String
    let payloadContentType: String
    let payloadFormat: AttachmentPayloadFormat
    let payload: Data
    let pixelWidth: Int?
    let pixelHeight: Int?
    let extractedCharacterCount: Int?
}
