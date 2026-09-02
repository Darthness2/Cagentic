import Foundation
import Testing
@testable import Cagentic

/// When the Latest button is offered.
///
/// It used to appear the moment the transcript sat more than 120pt from the end, which is roughly
/// one flick of momentum — so it turned up constantly without the reader having gone anywhere.
struct JumpToLatestTests {
    /// A typical phone viewport.
    static let viewport: CGFloat = 800

    private func distance(_ fromBottom: CGFloat) -> ScrollBottomDistance {
        ScrollBottomDistance(fromBottom: fromBottom, viewport: Self.viewport)
    }

    @Test("Barely leaving the bottom does not summon it")
    func staysHiddenForSmallOffsets() {
        for offset in [0, 40, 120, 250, 399] as [CGFloat] {
            #expect(
                distance(offset).showsJumpButton(whenAlreadyShowing: false) == false,
                "\(offset)pt should not be enough"
            )
        }
    }

    @Test("A deliberate scroll does summon it")
    func appearsAfterHalfAScreen() {
        #expect(distance(401).showsJumpButton(whenAlreadyShowing: false))
        #expect(distance(1_200).showsJumpButton(whenAlreadyShowing: false))
    }

    @Test("Once showing it survives well past the point that summoned it")
    func hasHysteresis() {
        // The failure this prevents: sitting exactly on one threshold and flickering.
        let boundary = distance(401)
        #expect(boundary.showsJumpButton(whenAlreadyShowing: false))
        #expect(boundary.showsJumpButton(whenAlreadyShowing: true))

        #expect(distance(300).showsJumpButton(whenAlreadyShowing: true))
        #expect(distance(200).showsJumpButton(whenAlreadyShowing: true))
        // ...but going most of the way back does dismiss it.
        #expect(distance(100).showsJumpButton(whenAlreadyShowing: true) == false)
    }

    @Test("A short viewport still needs a real scroll")
    func aFloorAppliesOnSmallScreens() {
        // Half of a small viewport would be a couple of finger-widths, so the floor takes over.
        let small = ScrollBottomDistance(fromBottom: 300, viewport: 420)
        #expect(small.showsJumpButton(whenAlreadyShowing: false) == false)

        let past = ScrollBottomDistance(fromBottom: 340, viewport: 420)
        #expect(past.showsJumpButton(whenAlreadyShowing: false))
    }

    @Test("The threshold scales with the screen")
    func scalesWithViewport() {
        let phone = ScrollBottomDistance(fromBottom: 500, viewport: 800)
        let pad = ScrollBottomDistance(fromBottom: 500, viewport: 1_300)
        #expect(phone.showsJumpButton(whenAlreadyShowing: false))
        // The same 500pt is less than half an iPad's transcript, so it has not earned the button.
        #expect(pad.showsJumpButton(whenAlreadyShowing: false) == false)
    }
}
