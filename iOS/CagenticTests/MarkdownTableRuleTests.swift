import SwiftUI
import Testing
import UIKit
@testable import Cagentic

/// Where a table's rules are actually drawn.
///
/// `Divider()` chooses its axis from the layout it finds itself in, and an `overlay` is not a
/// stack — so a divider meant to separate two columns drew a *horizontal* hairline, vertically
/// centred, straight through the text of every cell. Nothing about that is visible in a diff, and
/// a geometry assertion cannot see it either, so this renders the real view and looks at pixels.
@MainActor
struct MarkdownTableRuleTests {
    static let source = """
    | Method | Region | Time |
    |---|---|---|
    | Pour-over | Japan | 3 min |
    | French Press | France | 4 min |
    | Espresso | Italy | 25 sec |
    """
    /// Header plus three body rows.
    static let rowCount = 4

    /// Rows of pixels that are almost entirely non-background across the table's width. Cell text
    /// never covers more than a fraction of a line; a rule covers essentially all of it.
    private func horizontalRuleBands() async throws -> (bands: [ClosedRange<Int>], height: Int) {
        let image = try await render()
        let cgImage = try #require(image.cgImage)
        let width = cgImage.width
        let height = cgImage.height

        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let context = try #require(
            CGContext(
                data: &pixels,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: width * 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            )
        )
        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        func luminance(_ x: Int, _ y: Int) -> Int {
            let index = (y * width + x) * 4
            return (Int(pixels[index]) * 299 + Int(pixels[index + 1]) * 587
                + Int(pixels[index + 2]) * 114) / 1000
        }

        // The table is the only thing on a black backdrop, so its bounds are the lit region.
        var minX = width, maxX = 0, minY = height, maxY = 0
        for y in 0..<height {
            for x in 0..<width where luminance(x, y) > 12 {
                minX = min(minX, x); maxX = max(maxX, x)
                minY = min(minY, y); maxY = max(maxY, y)
            }
        }
        try #require(maxX > minX && maxY > minY, "nothing was rendered")

        // Sample inside the horizontal extent so the rounded corners cannot skew the coverage.
        let inset = (maxX - minX) / 12
        let span = (minX + inset)...(maxX - inset)
        var ruleRows: [Int] = []
        for y in minY...maxY {
            let lit = span.reduce(into: 0) { count, x in
                if luminance(x, y) > 34 { count += 1 }
            }
            if Double(lit) / Double(span.count) > 0.7 { ruleRows.append(y) }
        }

        var bands: [ClosedRange<Int>] = []
        for row in ruleRows {
            if let last = bands.last, row <= last.upperBound + 2 {
                bands[bands.count - 1] = last.lowerBound...row
            } else {
                bands.append(row...row)
            }
        }
        // Relative to the top of the table, so a caller can divide by the row height directly.
        return (bands.map { ($0.lowerBound - minY)...($0.upperBound - minY) }, maxY - minY + 1)
    }

    private func render() async throws -> UIImage {
        let host = UIHostingController(
            rootView: MarkdownText(Self.source)
                .padding(16)
                .frame(width: 380)
                .background(Color.black)
                .environment(\.colorScheme, .dark)
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 380, height: 700))
        window.rootViewController = host
        // Everything outside the table has to be true black, or the lit-region scan below finds
        // the window rather than the table.
        host.view.backgroundColor = .black
        window.backgroundColor = .black
        window.isHidden = false
        window.layoutIfNeeded()

        // Markdown is parsed off the main actor, so the first layout shows the plain-text fallback.
        for _ in 0..<40 {
            try await Task.sleep(for: .milliseconds(25))
            window.layoutIfNeeded()
        }

        let size = host.view.bounds.size
        return UIGraphicsImageRenderer(size: size).image { _ in
            host.view.drawHierarchy(in: CGRect(origin: .zero, size: size), afterScreenUpdates: true)
        }
    }

    @Test("No rule is drawn through the middle of a row")
    func noRuleCrossesTheText() async throws {
        let (bands, height) = try await horizontalRuleBands()
        let rowHeight = Double(height) / Double(Self.rowCount)

        // The band a rule must never fall in: the middle of a row, where the text sits.
        let offenders = bands.filter { band in
            let centre = Double(band.lowerBound + band.upperBound) / 2
            let withinRow = centre.truncatingRemainder(dividingBy: rowHeight) / rowHeight
            return withinRow > 0.25 && withinRow < 0.75
        }
        #expect(offenders.isEmpty, "a hairline crosses the text: \(offenders) of \(bands)")
    }

    @Test("Rows are still separated")
    func rulesExistAtRowBoundaries() async throws {
        let (bands, _) = try await horizontalRuleBands()
        // Guards the test above from passing because nothing was drawn at all: the boundaries
        // between the four rows, plus the table's own top and bottom edge.
        #expect(bands.count >= Self.rowCount - 1, "expected row separators, found \(bands)")
    }
}
