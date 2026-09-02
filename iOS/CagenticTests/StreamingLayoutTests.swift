import SwiftUI
import Testing
import UIKit
@testable import Cagentic

/// How a reply behaves while it is still arriving.
///
/// Markdown is parsed off the main actor, so between parses the view holds a slightly stale
/// document plus whatever has streamed in since. That remainder used to render as a block of its
/// own, a full stack spacing below the paragraph it belonged to: each word appeared underneath the
/// message and jumped up when the parse landed, and the block winking in and out changed the
/// message's height by a line plus the spacing several times a second — which bounced the whole
/// transcript, since it is pinned to the bottom while generating.
///
/// The invariant is that a reply only ever grows. Measuring only settled frames would miss this
/// entirely, because the flicker lives in the frames *between* parses; each step here is therefore
/// measured twice, once after the parse has landed and once immediately after new text arrives.
@MainActor
struct StreamingLayoutTests {
    static let width: CGFloat = 340

    private final class Rig {
        let host = UIHostingController(rootView: AnyView(EmptyView()))
        let window: UIWindow

        init() {
            window = UIWindow(frame: CGRect(x: 0, y: 0, width: width, height: 900))
            window.rootViewController = host
            window.isHidden = false
        }

        func show(_ text: String) {
            host.rootView = AnyView(
                MarkdownText(text).frame(width: width, alignment: .leading)
            )
        }

        func height() -> CGFloat {
            window.layoutIfNeeded()
            return host.view.systemLayoutSizeFitting(
                CGSize(width: width, height: UIView.layoutFittingCompressedSize.height),
                withHorizontalFittingPriority: .required,
                verticalFittingPriority: .fittingSizeLevel
            ).height
        }
    }

    /// Replays `text` the way the stream delivers it, recording the height after the parse has
    /// caught up and again while a fresh chunk is still unparsed.
    private func heights(_ text: String, chunk: Int) async throws -> [CGFloat] {
        let rig = Rig()
        var measured: [CGFloat] = []
        var count = min(24, text.count)

        while count < text.count {
            let settledEnd = text.index(text.startIndex, offsetBy: count)
            rig.show(String(text[text.startIndex..<settledEnd]))
            try await Task.sleep(for: .milliseconds(30))
            measured.append(rig.height())

            count = min(count + chunk, text.count)
            let pendingEnd = text.index(text.startIndex, offsetBy: count)
            rig.show(String(text[text.startIndex..<pendingEnd]))
            // Deliberately no sleep: this is the frame where the tail has arrived and the parser
            // has not run yet, which is the one that used to be a full block taller.
            measured.append(rig.height())
        }
        return measured
    }

    private func shrinks(_ measured: [CGFloat]) -> [(Int, CGFloat, CGFloat)] {
        zip(measured, measured.dropFirst())
            .enumerated()
            .filter { $0.element.1 < $0.element.0 - 0.5 }
            .map { ($0.offset, $0.element.0, $0.element.1) }
    }

    @Test("A paragraph never gets shorter while it streams")
    func paragraphOnlyGrows() async throws {
        let text = """
        Here is the answer you asked for, written out at enough length that it wraps across \
        several lines on a phone and gives the parser something to chew on while the rest of \
        the words are still arriving one at a time.
        """
        let measured = try await heights(text, chunk: 8)
        try #require(measured.count > 12, "the replay did not produce enough frames")
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("Crossing into a second paragraph does not bounce either")
    func multipleParagraphsOnlyGrow() async throws {
        let text = """
        A first paragraph long enough to wrap onto a second line before it ends here.

        And a second one after it, so the boundary between two blocks is exercised while the \
        text is still coming in.
        """
        let measured = try await heights(text, chunk: 8)
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("A heading does not arrive as loose text first")
    func headingsOnlyGrow() async throws {
        let measured = try await heights(
            """
            Some prose before it.

            ## A heading in the answer

            And prose after it that runs on far enough to wrap.
            """,
            chunk: 6
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("A list does not arrive as loose text first")
    func listsOnlyGrow() async throws {
        let measured = try await heights(
            """
            Steps to follow:

            1. the first step, written out
            2. the second step
            3. the third step

            That is all.
            """,
            chunk: 6
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("A code block does not arrive as loose text first")
    func codeBlocksOnlyGrow() async throws {
        // The worst case before: every line of code appeared below the block, then jumped inside.
        let measured = try await heights(
            """
            Here is the code:

            ```swift
            func go(_ x: Int) -> Int {
                return x + 1
            }
            ```

            Done.
            """,
            chunk: 6
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("A table does not arrive as loose text first")
    func tablesOnlyGrow() async throws {
        let measured = try await heights(
            """
            | Method | Region |
            |---|---|
            | Pour-over | Japan |
            | Espresso | Italy |
            """,
            chunk: 5
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("Bold and italic do not shift the line as they complete")
    func inlineEmphasisOnlyGrows() async throws {
        let measured = try await heights(
            """
            This sentence has **bold text** and *italic text* and `inline code` in it, long \
            enough that it wraps onto a second line before the end.
            """,
            chunk: 4
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("A reply mixing every construct still only grows")
    func mixedContentOnlyGrows() async throws {
        let measured = try await heights(
            """
            ## Summary

            A paragraph with **bold** in it.

            - first
            - second

            ```python
            print("hi")
            ```

            | k | v |
            |---|---|
            | a | 1 |

            Closing line.
            """,
            chunk: 7
        )
        #expect(shrinks(measured).isEmpty, "the message shrank mid-stream: \(shrinks(measured))")
    }

    @Test("The measurement actually sees the message")
    func theRigRendersSomething() async throws {
        // Guards every test above: a rig that measured an empty view would never shrink.
        let rig = Rig()
        rig.show(String(repeating: "Words that wrap. ", count: 12))
        for _ in 0..<20 {
            try await Task.sleep(for: .milliseconds(25))
            _ = rig.height()
        }
        #expect(rig.height() > 100)
    }
}
