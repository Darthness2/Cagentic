import Testing
@testable import Cagentic

struct GatewayTextSanitizerTests {
    @Test("Text with no reasoning is returned unchanged")
    func passesThroughPlainText() {
        let split = GatewayTextSanitizer.split("The file has 12 lines.")

        #expect(split.answer == "The file has 12 lines.")
        #expect(split.reasoning.isEmpty)
    }

    @Test("A closed thinking block is separated from the answer")
    func extractsClosedThinkingBlock() {
        let split = GatewayTextSanitizer.split(
            "<think>The user wants a count.</think>The file has 12 lines."
        )

        #expect(split.answer == "The file has 12 lines.")
        #expect(split.reasoning == "The user wants a count.")
    }

    @Test("Both tag spellings are recognised, in any case")
    func recognisesBothTagSpellings() {
        #expect(GatewayTextSanitizer.split("<thinking>a</thinking>b").reasoning == "a")
        #expect(GatewayTextSanitizer.split("<THINK>a</THINK>b").reasoning == "a")
    }

    @Test("Several blocks are joined in order")
    func joinsMultipleBlocks() {
        let split = GatewayTextSanitizer.split("<think>one</think>A<think>two</think>B")

        #expect(split.answer == "AB")
        #expect(split.reasoning == "one\n\ntwo")
    }

    @Test("A block still being streamed is treated as reasoning so far")
    func handlesUnterminatedBlock() {
        // This is the common case mid-stream: the closing tag has not arrived yet, and without this
        // the raw tag and the model's private reasoning would appear in the answer.
        let split = GatewayTextSanitizer.split("Sure.<think>They probably mean the second")

        #expect(split.answer == "Sure.")
        #expect(split.reasoning == "They probably mean the second")
    }

    @Test("A tag split across two deltas is handled once both have arrived")
    func handlesTagSplitAcrossDeltas() {
        // The sanitizer runs over the whole accumulated buffer for exactly this reason.
        var buffer = "Checking.<thi"
        #expect(GatewayTextSanitizer.split(buffer).answer == "Checking.<thi")

        buffer += "nk>hmm</think> Done."
        let split = GatewayTextSanitizer.split(buffer)
        #expect(split.answer == "Checking. Done.")
        #expect(split.reasoning == "hmm")
    }

    @Test("HUD fences are stripped from the answer")
    func stripsHUDFences() {
        let split = GatewayTextSanitizer.split(
            "Here is the breakdown.\n```hud\n{\"type\":\"bar\",\"values\":[1,2]}\n```\nThat's it."
        )

        #expect(!split.answer.contains("hud"))
        #expect(!split.answer.contains("\"type\""))
        #expect(split.answer.contains("Here is the breakdown."))
        #expect(split.answer.contains("That's it."))
    }

    @Test("An unterminated HUD fence does not leak JSON mid-stream")
    func stripsUnterminatedHUDFence() {
        let split = GatewayTextSanitizer.split("Result:\n```hud\n{\"type\":\"bar\"")

        #expect(split.answer == "Result:")
    }

    @Test("Ordinary code fences are preserved")
    func preservesOrdinaryCodeFences() {
        let source = "Run this:\n```bash\nls -la\n```"
        let split = GatewayTextSanitizer.split(source)

        #expect(split.answer == source)
    }
}
