import SwiftUI

/// The Cagentic mark: a ring broken into four arcs.
///
/// Kept in step with `design-system/brand/make-icons.py`, which draws the same geometry for the
/// app icon, the browser-extension icons, and the web favicon. The constants are duplicated rather
/// than shared because there is no format both a Swift shape and a Python generator can read — so
/// if one changes, the other has to, and `BrandMarkTests` is what notices.
nonisolated struct MarkShape: Shape {
    /// Degrees counter-clockwise from the positive x-axis, one per quarter.
    static let arcCentres: [Double] = [30, 120, 210, 300]
    /// A little under each arc's 90° share, which is what leaves the four gaps.
    static let arcSpan: Double = 65
    /// The ring's centre-line, as a fraction of the frame.
    static let radius: Double = 0.3486
    /// The stroke that rides on it, as a fraction of the frame.
    static let stroke: Double = 0.09375

    func path(in rect: CGRect) -> Path {
        let edge = min(rect.width, rect.height)
        let centre = CGPoint(x: rect.midX, y: rect.midY)
        let radius = edge * Self.radius

        var path = Path()
        for degrees in Self.arcCentres {
            // SwiftUI measures clockwise from 3 o'clock with y growing downward, so the
            // mathematical angles are negated and the ends swap to keep the sweep the same.
            let start = Angle.degrees(-(degrees + Self.arcSpan / 2))
            let end = Angle.degrees(-(degrees - Self.arcSpan / 2))
            // `addArc` draws a line from the current point to the arc's start, and four arcs
            // added to one path spoke together across the middle. Moving exactly onto the start
            // makes that line zero-length, which is what keeps them four separate strokes.
            path.move(
                to: CGPoint(
                    x: centre.x + radius * cos(start.radians),
                    y: centre.y + radius * sin(start.radians)
                )
            )
            path.addArc(
                center: centre,
                radius: radius,
                startAngle: start,
                endAngle: end,
                clockwise: false
            )
        }
        return path
    }
}

struct BrandMark: View {
    var size: CGFloat = 36

    var body: some View {
        MarkShape()
            .stroke(
                CagenticTheme.accent,
                style: StrokeStyle(
                    lineWidth: max(1.7, size * MarkShape.stroke),
                    lineCap: .round,
                    lineJoin: .round
                )
            )
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

struct BrandLockup: View {
    var compact = false

    var body: some View {
        HStack(spacing: CagenticTheme.Spacing.sm) {
            BrandMark(size: compact ? 24 : 30)
            VStack(alignment: .leading, spacing: 0) {
                Text("Cagentic")
                    .font(compact ? CagenticTheme.FontStyle.headline : CagenticTheme.FontStyle.displaySmall)
                    .foregroundStyle(CagenticTheme.textPrimary)
                if !compact {
                    Text("Local AI workspace")
                        .font(CagenticTheme.FontStyle.caption)
                        .foregroundStyle(CagenticTheme.textSecondary)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }
}

#Preview("Brand lockup") {
    BrandLockup()
        .padding()
        .background(CagenticTheme.background)
}

#Preview("Brand lockup · dark") {
    BrandLockup()
        .padding()
        .background(CagenticTheme.background)
        .preferredColorScheme(.dark)
}
