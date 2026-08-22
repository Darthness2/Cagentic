import Foundation
import Testing
@testable import Cagentic

struct GatewaySSEParserTests {
    @Test("A blank line dispatches the accumulated data field")
    func dispatchesFrameOnBlankLine() throws {
        var frames = try parse("data: {\"kind\":\"end\"}\n\n")

        #expect(frames == ["{\"kind\":\"end\"}"])

        frames = try parse("data: one\n\ndata: two\n\n")
        #expect(frames == ["one", "two"])
    }

    @Test("CRLF line endings are tolerated")
    func toleratesCarriageReturns() throws {
        let frames = try parse("data: {\"kind\":\"end\"}\r\n\r\n")

        #expect(frames == ["{\"kind\":\"end\"}"])
    }

    @Test("Multiple data fields in one frame are joined with newlines")
    func joinsMultipleDataFields() throws {
        let frames = try parse("data: first\ndata: second\n\n")

        #expect(frames == ["first\nsecond"])
    }

    @Test("Comments and unknown fields are ignored")
    func ignoresCommentsAndUnknownFields() throws {
        let frames = try parse(": keep-alive\nevent: message\nid: 7\ndata: payload\n\n")

        #expect(frames == ["payload"])
    }

    @Test("A frame left unterminated by a dropped connection is still delivered")
    func flushesUnterminatedFrame() throws {
        var parser = GatewaySSEParser(maximumFrameBytes: 1_024, maximumAggregateBytes: 4_096)
        var frames: [String] = []
        for byte in Array("data: partial\n".utf8) {
            if let frame = try parser.append(byte) {
                frames.append(frame)
            }
        }
        #expect(frames.isEmpty)

        let final = try parser.finish()
        #expect(final == "partial")
    }

    @Test("An oversized frame is refused rather than buffered without limit")
    func refusesOversizedFrame() {
        var parser = GatewaySSEParser(maximumFrameBytes: 16, maximumAggregateBytes: 4_096)
        #expect(throws: GatewayClientError.streamFrameTooLarge(limitBytes: 16)) {
            for byte in Array("data: \(String(repeating: "x", count: 64))\n\n".utf8) {
                _ = try parser.append(byte)
            }
        }
    }

    @Test("An oversized response is refused")
    func refusesOversizedResponse() {
        var parser = GatewaySSEParser(maximumFrameBytes: 1_024, maximumAggregateBytes: 8)
        #expect(throws: GatewayClientError.streamResponseTooLarge(limitBytes: 8)) {
            for byte in Array("data: hello there\n\n".utf8) {
                _ = try parser.append(byte)
            }
        }
    }

    private func parse(_ text: String) throws -> [String] {
        var parser = GatewaySSEParser(maximumFrameBytes: 4_096, maximumAggregateBytes: 65_536)
        var frames: [String] = []
        for byte in Array(text.utf8) {
            if let frame = try parser.append(byte) {
                frames.append(frame)
            }
        }
        return frames
    }
}

struct GatewayEventDecoderTests {
    private let decoder = GatewayEventDecoder()

    @Test("Deltas append and assistant text replaces")
    func decodesTextEvents() throws {
        #expect(try decoder.decode(frame: #"{"kind":"delta","data":{"text":"Hel"}}"#)
            == [.contentDelta("Hel")])
        #expect(try decoder.decode(frame: #"{"kind":"assistant","data":{"text":"Hello."}}"#)
            == [.contentReplace("Hello.")])
        // Emitted instead of `assistant` when the gateway's engine has streaming off; identical
        // meaning, so it must decode identically.
        #expect(try decoder.decode(frame: #"{"kind":"narration","data":{"text":"Hello."}}"#)
            == [.contentReplace("Hello.")])
        #expect(try decoder.decode(frame: #"{"kind":"delta","data":{"text":""}}"#) == [])
    }

    @Test("A plan decodes to its steps")
    func decodesPlan() throws {
        let events = try decoder.decode(
            frame: #"{"kind":"plan","data":{"steps":["Read the file","Patch it",""]}}"#
        )

        #expect(events == [.plan(["Read the file", "Patch it"])])
    }

    @Test("Tool calls and results decode with their identifiers")
    func decodesToolEvents() throws {
        let call = try decoder.decode(
            frame: #"{"kind":"tool_call","data":{"id":"t1","name":"read_file","args":{"path":"a.txt","nested":{"deep":[1,2]}},"summary":"a.txt"}}"#
        )
        #expect(call == [.toolCall(GatewayToolCall(id: "t1", name: "read_file", summary: "a.txt"))])

        let result = try decoder.decode(
            frame: #"{"kind":"tool_result","data":{"id":"t1","name":"read_file","ok":true,"first_line":"hello","result":"hello\nworld"}}"#
        )
        #expect(result == [
            .toolOutcome(
                GatewayToolOutcome(id: "t1", name: "read_file", isSuccess: true, firstLine: "hello")
            ),
        ])
    }

    @Test("A denial decodes to nothing, because its failed result follows")
    func skipsToolDenied() throws {
        let events = try decoder.decode(
            frame: #"{"kind":"tool_denied","data":{"id":"t1","name":"run_bash","reason":"user denied"}}"#
        )

        #expect(events.isEmpty)
    }

    @Test("A permission request decodes with everything needed to answer it")
    func decodesPermissionRequest() throws {
        let events = try decoder.decode(
            frame: #"{"kind":"permission","data":{"id":"p1","tool":"run_bash","summary":"git status","rule":"run_bash(git status*)","sandbox":"seatbelt · no network","network":false}}"#
        )

        #expect(events == [
            .permissionRequest(
                GatewayPermissionRequest(
                    id: "p1",
                    tool: "run_bash",
                    summary: "git status",
                    diff: nil,
                    rule: "run_bash(git status*)",
                    sandbox: "seatbelt · no network",
                    allowsNetwork: false
                )
            ),
        ])
    }

    @Test("The per-turn token telemetry line is not shown as conversation content")
    func suppressesInternalTelemetry() throws {
        #expect(try decoder.decode(
            frame: #"{"kind":"info","data":{"text":"sending ~12,004 tokens to qwen2.5:7b · 24 tools"}}"#
        ).isEmpty)
        #expect(try decoder.decode(
            frame: #"{"kind":"info","data":{"text":"attached 2 file(s) to this turn"}}"#
        ) == [.notice(level: .info, text: "attached 2 file(s) to this turn")])
    }

    @Test("A warning decodes as a warning notice")
    func decodesWarning() throws {
        #expect(try decoder.decode(
            frame: #"{"kind":"warn","data":{"text":"loop detected on read_file — steered."}}"#
        ) == [.notice(level: .warning, text: "loop detected on read_file — steered.")])
    }

    @Test("A refusal is distinguished from a mid-turn failure")
    func separatesRejectionFromFailure() {
        #expect(throws: GatewayClientError.busy(
            message: "Cagentic is still working on the previous message."
        )) {
            try decoder.decode(
                frame: #"{"kind":"error","data":{"text":"Cagentic is still working on the previous message."}}"#
            )
        }
        #expect(throws: GatewayClientError.server(message: "RuntimeError: boom")) {
            try decoder.decode(frame: #"{"kind":"error","data":{"text":"RuntimeError: boom"}}"#)
        }
    }

    @Test("Completion reports this turn's usage, not the session total")
    func decodesCompletion() throws {
        let events = try decoder.decode(
            frame: #"{"kind":"done","data":{"text":"Done.","usage":{"input":900,"output":90,"ms":9000},"turn_usage":{"input":120,"output":40,"ms":2500}}}"#
        )

        #expect(events == [
            .completed(
                GatewayTurnSummary(
                    text: "Done.",
                    usage: GatewayTurnUsage(inputTokens: 120, outputTokens: 40, milliseconds: 2_500)
                )
            ),
        ])
    }

    @Test("Compaction decodes with its before and after sizes")
    func decodesCompaction() throws {
        #expect(try decoder.decode(
            frame: #"{"kind":"compact","data":{"strategy":"bulletize","before":40000,"after":12000}}"#
        ) == [.compacted(before: 40_000, after: 12_000)])
    }

    @Test("The terminating frame decodes to the end event")
    func decodesEnd() throws {
        #expect(try decoder.decode(frame: #"{"kind":"end","data":{}}"#) == [.ended])
    }

    @Test("Kinds the app does not render decode to nothing")
    func ignoresUnrenderedKinds() throws {
        #expect(try decoder.decode(frame: #"{"kind":"user","data":{"text":"hi"}}"#).isEmpty)
        #expect(try decoder.decode(
            frame: #"{"kind":"widget","data":{"type":"bar","title":"t","data":{}}}"#
        ).isEmpty)
        // A kind added by a newer gateway must not break an older client.
        #expect(try decoder.decode(frame: #"{"kind":"telepathy","data":{}}"#).isEmpty)
        #expect(try decoder.decode(frame: "   ").isEmpty)
    }

    @Test("A frame that is not valid JSON is reported as malformed")
    func rejectsMalformedFrame() {
        #expect(throws: GatewayClientError.self) {
            try decoder.decode(frame: "{not json")
        }
    }
}
