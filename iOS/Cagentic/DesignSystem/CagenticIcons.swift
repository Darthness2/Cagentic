import SwiftUI

/// Chrome icons drawn in the brand's own language.
///
/// The mark is four arcs: one stroke weight, round caps, geometry rather than illustration. SF
/// Symbols beside it read as somebody else's set — `gearshape` and `square.and.pencil` in
/// particular are the two most recognisable glyphs on the platform, so the app's own frame looked
/// like scaffolding around the one part that was designed.
///
/// These share the mark's proportions: every shape is described on a 24-unit grid and stroked at
/// `MarkShape.stroke` of the frame, so an icon beside the mark at the same size carries the same
/// weight. Only the handful the app leans on are here — an SF Symbol is still the right answer for
/// anything incidental, and replacing the whole set would be a lot of drawing to end up with a
/// worse `trash`.
enum CagenticIcon {
    /// Matches `BrandMark`: line weight is a fraction of the frame, so it holds at any size.
    static func lineWidth(for size: CGFloat) -> CGFloat {
        max(1.5, size * MarkShape.stroke * 0.78)
    }
}

/// A panel with its leading column drawn in — the drawer this opens.
nonisolated struct SidebarIconShape: Shape {
    func path(in rect: CGRect) -> Path {
        let edge = min(rect.width, rect.height)
        let inset = edge * 0.115
        let frame = CGRect(
            x: rect.midX - edge / 2 + inset,
            y: rect.midY - edge / 2 + inset,
            width: edge - inset * 2,
            height: edge - inset * 2
        )
        var path = Path()
        path.addRoundedRect(in: frame, cornerSize: CGSize(width: edge * 0.2, height: edge * 0.2))
        // The divider sits a little past a third, which reads as a drawer rather than as a
        // rectangle cut in half.
        let split = frame.minX + frame.width * 0.38
        path.move(to: CGPoint(x: split, y: frame.minY))
        path.addLine(to: CGPoint(x: split, y: frame.maxY))
        return path
    }
}

/// A panel and a plus, built the same way the sidebar icon is: one closed form, one mark inside
/// it. An earlier attempt drew a speech bubble with a tail and a plus hung off the corner — two
/// competing elements that turned to mush at 19pt.
nonisolated struct NewChatIconShape: Shape {
    func path(in rect: CGRect) -> Path {
        let edge = min(rect.width, rect.height)
        let inset = edge * 0.115
        let frame = CGRect(
            x: rect.midX - edge / 2 + inset,
            y: rect.midY - edge / 2 + inset,
            width: edge - inset * 2,
            height: edge - inset * 2
        )
        var path = Path()
        path.addRoundedRect(in: frame, cornerSize: CGSize(width: edge * 0.2, height: edge * 0.2))
        let arm = frame.width * 0.26
        path.move(to: CGPoint(x: frame.midX - arm, y: frame.midY))
        path.addLine(to: CGPoint(x: frame.midX + arm, y: frame.midY))
        path.move(to: CGPoint(x: frame.midX, y: frame.midY - arm))
        path.addLine(to: CGPoint(x: frame.midX, y: frame.midY + arm))
        return path
    }
}

/// Renders one of the shapes above at the brand's weight.
struct CagenticIconView<S: Shape>: View {
    let shape: S
    var size: CGFloat = 22
    var fillsHandles = true

    var body: some View {
        shape
            .stroke(
                style: StrokeStyle(
                    lineWidth: CagenticIcon.lineWidth(for: size),
                    lineCap: .round,
                    lineJoin: .round
                )
            )
            .frame(width: size, height: size)
    }
}
