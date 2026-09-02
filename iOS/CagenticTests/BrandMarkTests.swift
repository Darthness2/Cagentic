import Foundation
import SwiftUI
import Testing
@testable import Cagentic

/// The mark's geometry, which lives in two places.
///
/// `MarkShape` draws it for the app, and `design-system/brand/make-icons.py` draws the same rings
/// for the app icon, the browser-extension icons, and the web favicon. There is no format both a
/// Swift shape and a Python generator can read, so the numbers are written twice — and two copies
/// of a constant drift silently. These compare them.
struct BrandMarkTests {
    static var generatorSource: String? {
        // Located from this file rather than a hardcoded path, so a checkout anywhere works.
        let repository = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // CagenticTests
            .deletingLastPathComponent()   // iOS
            .deletingLastPathComponent()   // repo root
        return try? String(
            contentsOf: repository.appending(path: "design-system/brand/make-icons.py"),
            encoding: .utf8
        )
    }

    private func number(_ label: String, in source: String) throws -> Double {
        // (?m) so ^ anchors to each line rather than the start of the file.
        let pattern = "(?m)^\(label) = ([0-9.]+)"
        let range = try #require(
            source.range(of: pattern, options: .regularExpression),
            "\(label) is not defined in make-icons.py"
        )
        let digits = source[range].split(separator: "=")[1].trimmingCharacters(in: .whitespaces)
        return try #require(Double(digits))
    }

    @Test("The app draws the same ring the icon generator does")
    func geometryMatchesTheGenerator() throws {
        let source = try #require(Self.generatorSource, "make-icons.py is missing")

        #expect(try number("ARC_SPAN", in: source) == MarkShape.arcSpan)
        #expect(try number("RADIUS", in: source) == MarkShape.radius)
        #expect(try number("STROKE", in: source) == MarkShape.stroke)

        let centres = try #require(source.range(of: "ARC_CENTRES = \\([^)]*\\)", options: .regularExpression))
        let parsed = source[centres]
            .split(separator: "(")[1]
            .split(separator: ",")
            .compactMap { Double($0.trimmingCharacters(in: CharacterSet(charactersIn: " )"))) }
        #expect(parsed == MarkShape.arcCentres)
    }

    @Test("Four arcs, on the quarters, with a gap between each")
    func theShapeIsARingOfFourArcs() {
        #expect(MarkShape.arcCentres.count == 4)
        // Evenly spaced, so the gaps are too.
        let gaps = zip(MarkShape.arcCentres, MarkShape.arcCentres.dropFirst()).map { $1 - $0 }
        #expect(gaps.allSatisfy { $0 == 90 })
        // Narrower than its quarter share, or the arcs meet and it is just a circle.
        #expect(MarkShape.arcSpan > 0 && MarkShape.arcSpan < 90)
    }

    @Test("The arcs are drawn as four separate strokes")
    func arcsDoNotSpokeTogether() {
        // `addArc` always emits a line from the current point to the arc's start, so four arcs
        // added to one path drew a line across the middle each time and the ring came out as a
        // wheel with spokes. The fix is to move onto the start first; what has to hold is that
        // every one of those lines has no length.
        let path = MarkShape().path(in: CGRect(x: 0, y: 0, width: 100, height: 100))
        var moves = 0
        var current = CGPoint.zero
        var spokes: [CGFloat] = []
        path.forEach { element in
            switch element {
            case let .move(to):
                moves += 1
                current = to
            case let .line(to):
                spokes.append(hypot(to.x - current.x, to.y - current.y))
                current = to
            case let .curve(to, _, _):
                current = to
            case let .quadCurve(to, _):
                current = to
            case .closeSubpath:
                break
            }
        }
        #expect(moves == MarkShape.arcCentres.count)
        #expect(spokes.allSatisfy { $0 < 0.01 }, "the arcs are joined across the middle: \(spokes)")
    }

    @Test("The mark is drawn in the appearance the app is in")
    @MainActor
    func theMarkFollowsTheAppearance() throws {
        // The app icon on the Home Screen switches because the asset catalog carries a dark
        // appearance. Inside the app the mark is stroked in the accent, which is a dynamic colour
        // — this is what checks it actually resolves differently rather than being pinned.
        func rendered(_ scheme: ColorScheme) throws -> Data {
            let host = UIHostingController(
                rootView: BrandMark(size: 64)
                    .padding(8)
                    .background(CagenticTheme.background)
                    .environment(\.colorScheme, scheme)
            )
            let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 80, height: 80))
            window.rootViewController = host
            window.overrideUserInterfaceStyle = scheme == .dark ? .dark : .light
            window.isHidden = false
            window.layoutIfNeeded()
            let image = UIGraphicsImageRenderer(size: host.view.bounds.size).image { context in
                host.view.layer.render(in: context.cgContext)
            }
            return try #require(image.pngData())
        }

        #expect(try rendered(.light) != rendered(.dark))
    }

    @Test("The mark stays inside its frame")
    func theMarkFitsItsFrame() {
        let side: CGFloat = 100
        let path = MarkShape().path(in: CGRect(x: 0, y: 0, width: side, height: side))
        // Half the stroke rides outside the centre-line, so the bounds plus that must still fit.
        let overhang = side * MarkShape.stroke / 2
        let bounds = path.boundingRect
        #expect(bounds.minX - overhang >= -0.01)
        #expect(bounds.minY - overhang >= -0.01)
        #expect(bounds.maxX + overhang <= side + 0.01)
        #expect(bounds.maxY + overhang <= side + 0.01)
    }
}
