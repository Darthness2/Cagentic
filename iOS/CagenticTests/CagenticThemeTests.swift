import SwiftUI
import Testing
import UIKit
@testable import Cagentic

struct CagenticThemeTests {
    @Test("Bundled Inter variable fonts are registered")
    @MainActor
    func interFontsAreRegistered() {
        #expect(UIFont(name: "InterVariable", size: 17) != nil)
        #expect(UIFont(name: "InterVariable-Medium", size: 17) != nil)
        #expect(UIFont(name: "InterVariable-SemiBold", size: 17) != nil)
        #expect(UIFont(name: "InterVariable-Bold", size: 17) != nil)
        #expect(UIFont(name: "InterVariableItalic", size: 17) != nil)
    }

    @Test("Dynamic theme colors resolve away from the main actor")
    func dynamicColorsResolveOffMainActor() async {
        let resolvedComponents = await Task.detached {
            let color = UIColor(Color(light: "112233", dark: "AABBCC"))
            let light = color.resolvedColor(
                with: UITraitCollection(userInterfaceStyle: .light)
            )
            let dark = color.resolvedColor(
                with: UITraitCollection(userInterfaceStyle: .dark)
            )

            return [light, dark].map { resolved in
                var red: CGFloat = 0
                var green: CGFloat = 0
                var blue: CGFloat = 0
                var alpha: CGFloat = 0
                resolved.getRed(&red, green: &green, blue: &blue, alpha: &alpha)
                return [red, green, blue, alpha]
            }
        }.value

        #expect(resolvedComponents.count == 2)
        #expect(resolvedComponents[0][0] < resolvedComponents[1][0])
        #expect(resolvedComponents[0][3] == 1)
        #expect(resolvedComponents[1][3] == 1)
    }
}
