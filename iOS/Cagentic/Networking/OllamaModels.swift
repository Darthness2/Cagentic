import Foundation

public nonisolated struct OllamaServerVersion: Codable, Equatable, Sendable {
    public let version: String

    public init(version: String) {
        self.version = version
    }
}

public nonisolated struct OllamaModel: Codable, Identifiable, Equatable, Sendable {
    public var id: String { name }

    public let name: String
    public let model: String?
    public let modifiedAt: String?
    public let size: Int64?
    public let digest: String?
    public let details: OllamaModelDetails?

    public init(
        name: String,
        model: String? = nil,
        modifiedAt: String? = nil,
        size: Int64? = nil,
        digest: String? = nil,
        details: OllamaModelDetails? = nil
    ) {
        self.name = name
        self.model = model
        self.modifiedAt = modifiedAt
        self.size = size
        self.digest = digest
        self.details = details
    }

    enum CodingKeys: String, CodingKey {
        case name
        case model
        case modifiedAt = "modified_at"
        case size
        case digest
        case details
    }
}

public nonisolated struct OllamaModelDetails: Codable, Equatable, Sendable {
    public let parentModel: String?
    public let format: String?
    public let family: String?
    public let families: [String]?
    public let parameterSize: String?
    public let quantizationLevel: String?

    public init(
        parentModel: String? = nil,
        format: String? = nil,
        family: String? = nil,
        families: [String]? = nil,
        parameterSize: String? = nil,
        quantizationLevel: String? = nil
    ) {
        self.parentModel = parentModel
        self.format = format
        self.family = family
        self.families = families
        self.parameterSize = parameterSize
        self.quantizationLevel = quantizationLevel
    }

    enum CodingKeys: String, CodingKey {
        case parentModel = "parent_model"
        case format
        case family
        case families
        case parameterSize = "parameter_size"
        case quantizationLevel = "quantization_level"
    }
}

nonisolated struct OllamaTagsResponse: Decodable, Sendable {
    let models: [OllamaModel]
}

public nonisolated struct OllamaShowResponse: Decodable, Equatable, Sendable {
    public let license: String?
    public let modelfile: String?
    public let parameters: String?
    public let template: String?
    public let system: String?
    public let modifiedAt: String?
    public let details: OllamaModelDetails?
    public let modelInfo: [String: JSONValue]?
    public let projectorInfo: [String: JSONValue]?
    public let capabilities: [String]?

    enum CodingKeys: String, CodingKey {
        case license
        case modelfile
        case parameters
        case template
        case system
        case modifiedAt = "modified_at"
        case details
        case modelInfo = "model_info"
        case projectorInfo = "projector_info"
        case capabilities
    }
}

public nonisolated struct OllamaPullResponse: Decodable, Equatable, Sendable {
    public let status: String

    public init(status: String) {
        self.status = status
    }
}

public nonisolated enum OllamaChatRole: String, Codable, CaseIterable, Sendable {
    case system
    case user
    case assistant
    case tool
}

public nonisolated struct OllamaChatMessage: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let role: OllamaChatRole
    public let content: String
    public let thinking: String?
    public let images: [String]?

    public init(
        id: UUID = UUID(),
        role: OllamaChatRole,
        content: String,
        thinking: String? = nil,
        images: [String]? = nil
    ) {
        self.id = id
        self.role = role
        self.content = content
        self.thinking = thinking
        self.images = images
    }

    enum CodingKeys: String, CodingKey {
        case role
        case content
        case thinking
        case images
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = UUID()
        role = try container.decode(OllamaChatRole.self, forKey: .role)
        content = try container.decodeIfPresent(String.self, forKey: .content) ?? ""
        thinking = try container.decodeIfPresent(String.self, forKey: .thinking)
        images = try container.decodeIfPresent([String].self, forKey: .images)
    }
}

public nonisolated struct OllamaChatOptions: Encodable, Equatable, Sendable {
    public var temperature: Double?
    public var topP: Double?
    public var topK: Int?
    public var seed: Int?
    public var contextLength: Int?
    public var maximumOutputTokens: Int?
    public var repeatPenalty: Double?
    public var stop: [String]?

    public init(
        temperature: Double? = nil,
        topP: Double? = nil,
        topK: Int? = nil,
        seed: Int? = nil,
        contextLength: Int? = nil,
        maximumOutputTokens: Int? = nil,
        repeatPenalty: Double? = nil,
        stop: [String]? = nil
    ) {
        self.temperature = temperature
        self.topP = topP
        self.topK = topK
        self.seed = seed
        self.contextLength = contextLength
        self.maximumOutputTokens = maximumOutputTokens
        self.repeatPenalty = repeatPenalty
        self.stop = stop
    }

    enum CodingKeys: String, CodingKey {
        case temperature
        case topP = "top_p"
        case topK = "top_k"
        case seed
        case contextLength = "num_ctx"
        case maximumOutputTokens = "num_predict"
        case repeatPenalty = "repeat_penalty"
        case stop
    }
}

public nonisolated struct OllamaChatRequest: Encodable, Equatable, Sendable {
    public let model: String
    public let messages: [OllamaChatMessage]
    public let options: OllamaChatOptions?
    public let keepAlive: String?
    public let think: Bool?

    public init(
        model: String,
        messages: [OllamaChatMessage],
        options: OllamaChatOptions? = nil,
        keepAlive: String? = nil,
        think: Bool? = nil
    ) {
        self.model = model
        self.messages = messages
        self.options = options
        self.keepAlive = keepAlive
        self.think = think
    }

    enum CodingKeys: String, CodingKey {
        case model
        case messages
        case stream
        case options
        case keepAlive = "keep_alive"
        case think
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(model, forKey: .model)
        try container.encode(messages, forKey: .messages)
        try container.encode(true, forKey: .stream)
        try container.encodeIfPresent(options, forKey: .options)
        try container.encodeIfPresent(keepAlive, forKey: .keepAlive)
        try container.encodeIfPresent(think, forKey: .think)
    }
}

public nonisolated struct OllamaChatMetrics: Equatable, Sendable {
    public let totalDurationNanoseconds: Int64?
    public let loadDurationNanoseconds: Int64?
    public let promptTokenCount: Int?
    public let promptEvaluationDurationNanoseconds: Int64?
    public let generatedTokenCount: Int?
    public let generationDurationNanoseconds: Int64?

    public init(
        totalDurationNanoseconds: Int64? = nil,
        loadDurationNanoseconds: Int64? = nil,
        promptTokenCount: Int? = nil,
        promptEvaluationDurationNanoseconds: Int64? = nil,
        generatedTokenCount: Int? = nil,
        generationDurationNanoseconds: Int64? = nil
    ) {
        self.totalDurationNanoseconds = totalDurationNanoseconds
        self.loadDurationNanoseconds = loadDurationNanoseconds
        self.promptTokenCount = promptTokenCount
        self.promptEvaluationDurationNanoseconds = promptEvaluationDurationNanoseconds
        self.generatedTokenCount = generatedTokenCount
        self.generationDurationNanoseconds = generationDurationNanoseconds
    }

    public var totalTokenCount: Int? {
        switch (promptTokenCount, generatedTokenCount) {
        case let (prompt?, generated?): prompt + generated
        case let (prompt?, nil): prompt
        case let (nil, generated?): generated
        case (nil, nil): nil
        }
    }

    public var generationTokensPerSecond: Double? {
        guard let generatedTokenCount,
              let generationDurationNanoseconds,
              generationDurationNanoseconds > 0
        else {
            return nil
        }
        return Double(generatedTokenCount) / (Double(generationDurationNanoseconds) / 1_000_000_000)
    }
}

public nonisolated struct OllamaChatCompletion: Equatable, Sendable {
    public let model: String?
    public let createdAt: String?
    public let doneReason: String?
    public let metrics: OllamaChatMetrics

    public init(
        model: String? = nil,
        createdAt: String? = nil,
        doneReason: String? = nil,
        metrics: OllamaChatMetrics
    ) {
        self.model = model
        self.createdAt = createdAt
        self.doneReason = doneReason
        self.metrics = metrics
    }
}

public nonisolated enum OllamaChatEvent: Equatable, Sendable {
    case thinking(String)
    case content(String)
    case completed(OllamaChatCompletion)
}

/// Codable storage for the heterogeneous `model_info` dictionaries returned by `/api/show`.
public nonisolated indirect enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case integer(Int64)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int64.self) {
            self = .integer(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value): try container.encode(value)
        case let .integer(value): try container.encode(value)
        case let .number(value): try container.encode(value)
        case let .bool(value): try container.encode(value)
        case let .object(value): try container.encode(value)
        case let .array(value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}
