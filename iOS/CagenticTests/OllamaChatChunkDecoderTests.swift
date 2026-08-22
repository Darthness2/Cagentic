import Foundation
import Testing
@testable import Cagentic

struct OllamaChatChunkDecoderTests {
    private let decoder = OllamaChatChunkDecoder()

    @Test("A chunk may deliver reasoning and visible content together")
    func decodesThinkingAndContentFromOneChunk() throws {
        let line = #"{"model":"deepseek-r1:8b","created_at":"2026-08-20T20:00:00Z","message":{"role":"assistant","thinking":"Check the premise.","content":"Here is the answer."},"done":false}"#

        #expect(
            try decoder.decode(line: line)
                == [.thinking("Check the premise."), .content("Here is the answer.")]
        )
    }

    @Test("Compatible servers may send thinking at the top level")
    func decodesTopLevelThinkingForCompatibleServers() throws {
        let line = #"{"thinking":"Working...","done":false}"#

        #expect(try decoder.decode(line: line) == [.thinking("Working...")])
    }

    @Test("The final record exposes token and nanosecond timing metrics")
    func decodesCompletionAndFinalMetrics() throws {
        let line = #"{"model":"qwen3:8b","created_at":"2026-08-20T20:00:01Z","done":true,"done_reason":"stop","total_duration":5000000000,"load_duration":1000000000,"prompt_eval_count":12,"prompt_eval_duration":500000000,"eval_count":20,"eval_duration":2000000000}"#

        let events = try decoder.decode(line: line)
        guard case let .completed(completion) = try #require(events.first) else {
            Issue.record("Expected a completion event")
            return
        }

        #expect(completion.model == "qwen3:8b")
        #expect(completion.createdAt == "2026-08-20T20:00:01Z")
        #expect(completion.doneReason == "stop")
        #expect(completion.metrics.totalDurationNanoseconds == 5_000_000_000)
        #expect(completion.metrics.loadDurationNanoseconds == 1_000_000_000)
        #expect(completion.metrics.promptTokenCount == 12)
        #expect(completion.metrics.generatedTokenCount == 20)
        #expect(completion.metrics.totalTokenCount == 32)
        #expect(abs((completion.metrics.generationTokensPerSecond ?? 0) - 10) < 0.0001)
    }

    @Test("An Ollama error record throws a typed server error")
    func serverErrorChunkThrowsTypedError() {
        do {
            _ = try decoder.decode(line: #"{"error":"model 'missing' not found"}"#)
            Issue.record("Expected the error chunk to throw")
        } catch let error as OllamaClientError {
            #expect(error == .server(message: "model 'missing' not found"))
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("Malformed NDJSON reports a typed error and safe excerpt")
    func malformedChunkThrowsTypedErrorWithExcerpt() {
        do {
            _ = try decoder.decode(line: #"{"message":broken}"#)
            Issue.record("Expected malformed JSON to throw")
        } catch let error as OllamaClientError {
            guard case let .malformedStreamChunk(excerpt, _) = error else {
                Issue.record("Expected malformedStreamChunk, got \(error)")
                return
            }
            #expect(excerpt == #"{"message":broken}"#)
        } catch {
            Issue.record("Expected OllamaClientError, got \(error)")
        }
    }

    @Test("Blank NDJSON separator lines are ignored")
    func blankLineIsIgnored() throws {
        #expect(try decoder.decode(line: "  \n").isEmpty)
    }
}
