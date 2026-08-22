import Foundation
import Testing
@testable import Cagentic

struct OllamaResponseDecodingTests {
    @Test("The lightweight version health response decodes")
    func decodesVersionResponse() throws {
        let response = try JSONDecoder().decode(
            OllamaServerVersion.self,
            from: Data(#"{"version":"0.11.7"}"#.utf8)
        )

        #expect(response == OllamaServerVersion(version: "0.11.7"))
    }

    @Test("The installed model list decodes useful display metadata")
    func decodesTagsResponse() throws {
        let json = #"{"models":[{"name":"qwen3:8b","model":"qwen3:8b","modified_at":"2026-08-20T20:00:00Z","size":5230000000,"digest":"abc123","details":{"format":"gguf","family":"qwen3","families":["qwen3"],"parameter_size":"8.2B","quantization_level":"Q4_K_M"}}]}"#

        let response = try JSONDecoder().decode(OllamaTagsResponse.self, from: Data(json.utf8))
        let model = try #require(response.models.first)

        #expect(model.name == "qwen3:8b")
        #expect(model.size == 5_230_000_000)
        #expect(model.details?.family == "qwen3")
        #expect(model.details?.parameterSize == "8.2B")
        #expect(model.details?.quantizationLevel == "Q4_K_M")
    }

    @Test("The show response preserves heterogeneous model information")
    func decodesShowResponseWithHeterogeneousModelInfo() throws {
        let json = #"{"license":"Apache-2.0","template":"{{ .Prompt }}","details":{"format":"gguf","family":"llama","parameter_size":"8B"},"model_info":{"general.architecture":"llama","llama.context_length":8192,"llama.rope.freq_base":10000.0,"supports.tools":true,"stop_tokens":[1,2,3]},"capabilities":["completion","tools"]}"#

        let response = try JSONDecoder().decode(OllamaShowResponse.self, from: Data(json.utf8))

        #expect(response.license == "Apache-2.0")
        #expect(response.details?.format == "gguf")
        #expect(response.capabilities == ["completion", "tools"])
        #expect(response.modelInfo?["general.architecture"] == .string("llama"))
        #expect(response.modelInfo?["llama.context_length"] == .integer(8_192))
        #expect(response.modelInfo?["supports.tools"] == .bool(true))
        #expect(
            response.modelInfo?["stop_tokens"]
                == .array([.integer(1), .integer(2), .integer(3)])
        )
    }

    @Test("Chat requests force streaming and encode Ollama option keys")
    func chatRequestEncodesStreamingAndSnakeCaseOptions() throws {
        let request = OllamaChatRequest(
            model: "qwen3:8b",
            messages: [.init(role: .user, content: "Hello")],
            options: .init(temperature: 0.4, contextLength: 16_384, maximumOutputTokens: 512),
            keepAlive: "15m",
            think: true
        )

        let data = try JSONEncoder().encode(request)
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let options = try #require(object["options"] as? [String: Any])

        #expect(object["model"] as? String == "qwen3:8b")
        #expect(object["stream"] as? Bool == true)
        #expect(object["keep_alive"] as? String == "15m")
        #expect(object["think"] as? Bool == true)
        #expect(options["num_ctx"] as? Int == 16_384)
        #expect(options["num_predict"] as? Int == 512)
        #expect(options["temperature"] as? Double == 0.4)
    }
}
