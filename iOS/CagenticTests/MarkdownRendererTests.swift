import Testing
@testable import Cagentic

struct MarkdownRendererTests {
    @Test("Tables, display math, and nested lists remain structural while streaming")
    func recognizesRichMarkdownBlocks() {
        let source = """
        - Parent
          - Child
            1. Grandchild

        | Item | Value |
        |:-----|------:|
        | A | 42 |

        $$
        f(x) = \\sum_{i=1}^{n} x_i
        $$
        """

        let summary = MarkdownFeatureInspector.inspect(source)

        #expect(summary.tableCount == 1)
        #expect(summary.mathBlockCount == 1)
        #expect(summary.maximumListDepth == 2)
    }

    @Test("An unfinished streamed equation renders as math immediately")
    func recognizesPartialDisplayMath() {
        let summary = MarkdownFeatureInspector.inspect(
            """
            $$
            E = mc^2
            """
        )

        #expect(summary.mathBlockCount == 1)
    }
}
