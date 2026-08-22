import SwiftUI

nonisolated struct SparkShape: Shape {
    func path(in rect: CGRect) -> Path {
        let scaleX = rect.width / 24
        let scaleY = rect.height / 24

        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + x * scaleX, y: rect.minY + y * scaleY)
        }

        var path = Path()
        path.move(to: point(12, 3))
        path.addCurve(
            to: point(21, 12),
            control1: point(12.7, 8.1),
            control2: point(15.9, 11.3)
        )
        path.addCurve(
            to: point(12, 21),
            control1: point(15.9, 12.7),
            control2: point(12.7, 15.9)
        )
        path.addCurve(
            to: point(3, 12),
            control1: point(11.3, 15.9),
            control2: point(8.1, 12.7)
        )
        path.addCurve(
            to: point(12, 3),
            control1: point(8.1, 11.3),
            control2: point(11.3, 8.1)
        )
        path.closeSubpath()
        return path
    }
}

struct BrandMark: View {
    var size: CGFloat = 36

    var body: some View {
        SparkShape()
            .stroke(
                CagenticTheme.accent,
                style: StrokeStyle(
                    lineWidth: max(1.7, size * 0.075),
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
