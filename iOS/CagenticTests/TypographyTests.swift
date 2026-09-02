import Foundation
import SwiftUI
import Testing
import UIKit
@testable import Cagentic

/// The bundled type stack.
///
/// `UIFont(name:)` returns nil for a face that is not registered and every call
/// site then quietly falls back to the system font, so a typo in a PostScript
/// name or a file missing from `UIAppFonts` looks completely normal on screen.
/// These check the wiring instead of the pixels.
struct TypographyTests {
    /// Every name the theme can ask for, after the family's own collapsing —
    /// `.code` only ships one weight, and only the reading face has an italic.
    static let reachableNames: Set<String> = {
        var names: Set<String> = []
        for family in CagenticFontFamily.allCases {
            for weight in CagenticFontWeight.allCases {
                for italic in [false, true] {
                    names.insert(
                        CagenticFontName.postScript(family, weight: weight, italic: italic)
                    )
                }
            }
        }
        return names
    }()

    static var registeredFileNames: [String] {
        // The test host is the app, so main is the app bundle, not the runner.
        let entries = Bundle.main.object(forInfoDictionaryKey: "UIAppFonts")
        return (entries as? [String]) ?? []
    }

    @Test("Every face the theme can ask for is actually registered")
    func everyReachableFaceLoads() throws {
        for name in Self.reachableNames {
            #expect(UIFont(name: name, size: 17) != nil, "\(name) is not a registered font")
        }
    }

    @Test("Nothing is bundled that the theme never asks for")
    func noUnusedWeightsAreShipped() throws {
        // The whole point of instancing static weights instead of shipping the
        // variable fonts: the bundle carries exactly the faces in use.
        let bundled = Set(Self.registeredFileNames.map { ($0 as NSString).deletingPathExtension })
        #expect(bundled == Self.reachableNames)
    }

    @Test("The three roles resolve to three different families")
    func rolesAreDistinct() throws {
        let ui = try #require(UIFont(name: CagenticFontName.postScript(.ui), size: 17))
        let reading = try #require(UIFont(name: CagenticFontName.postScript(.reading), size: 17))
        let code = try #require(UIFont(name: CagenticFontName.postScript(.code), size: 17))

        #expect(ui.familyName == "Space Grotesk")
        #expect(reading.familyName == "Plus Jakarta Sans")
        #expect(code.familyName == "JetBrains Mono")
    }

    @Test("The code face is monospaced and the others are not")
    func codeIsMonospaced() throws {
        func advance(_ font: UIFont, _ character: String) -> CGFloat {
            (character as NSString).size(withAttributes: [.font: font]).width
        }
        let code = try #require(UIFont(name: CagenticFontName.postScript(.code), size: 17))
        // The check that matters for output alignment: every glyph one width.
        #expect(abs(advance(code, "i") - advance(code, "W")) < 0.01)

        let reading = try #require(UIFont(name: CagenticFontName.postScript(.reading), size: 17))
        #expect(advance(reading, "i") < advance(reading, "W"))
    }

    @Test("Bold and italic resolve to real faces, not synthesised ones")
    func emphasisHasRealFaces() throws {
        // Markdown emphasis inside an answer goes through the family's traits
        // rather than an explicit PostScript name, so Regular/Bold/Italic have
        // to share one family name for the system to find them.
        let family = UIFont.fontNames(forFamilyName: "Plus Jakarta Sans")
        #expect(family.contains("PlusJakartaSans-Regular"))
        #expect(family.contains("PlusJakartaSans-Bold"))
        #expect(family.contains("PlusJakartaSans-Italic"))
    }

    /// Renders one string and hands back the pixels, so two font tokens can be
    /// compared for what they actually draw rather than for what they claim.
    @MainActor
    private func rendered(_ font: Font) throws -> Data {
        let view = Text("Handgloves 0123 — reply prose")
            .font(font)
            .frame(width: 400, alignment: .leading)
            .background(Color.white)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3
        let image = try #require(renderer.uiImage)
        return try #require(image.pngData())
    }

    @Test("Each role draws in the family it names")
    @MainActor
    func rolesDrawInTheRightFamily() throws {
        // Only ever compares two live renders against each other, so this does
        // not go stale when a new OS changes rasterisation.
        #expect(
            try rendered(CagenticTheme.FontStyle.Message.body)
                == rendered(.custom("PlusJakartaSans-Regular", size: 17, relativeTo: .body)),
            "message body is not Plus Jakarta Sans"
        )
        #expect(
            try rendered(CagenticTheme.FontStyle.body)
                == rendered(.custom("SpaceGrotesk-Regular", size: 17, relativeTo: .body)),
            "chrome body is not Space Grotesk"
        )
        #expect(
            try rendered(CagenticTheme.FontStyle.Code.inline)
                == rendered(.custom("JetBrainsMono-Regular", size: 17, relativeTo: .body)),
            "code is not JetBrains Mono"
        )
        // The regression this guards: routing an answer back to the chrome face
        // is invisible in a diff and obvious only side by side.
        #expect(
            try rendered(CagenticTheme.FontStyle.Message.body)
                != rendered(CagenticTheme.FontStyle.body)
        )
    }

    @Test("Weights the theme names map to the weight actually drawn")
    func weightsAreDistinct() throws {
        let weights: [CagenticFontWeight] = [.regular, .medium, .semibold, .bold]
        let measured = try weights.map { weight -> CGFloat in
            let name = CagenticFontName.postScript(.ui, weight: weight)
            let font = try #require(UIFont(name: name, size: 17))
            let traits = font.fontDescriptor.object(forKey: .traits) as? [UIFontDescriptor.TraitKey: Any]
            return (traits?[.weight] as? CGFloat) ?? .nan
        }
        // Strictly increasing: four names that all resolved to the same outline
        // would mean the instancer pinned the wrong axis value.
        #expect(zip(measured, measured.dropFirst()).allSatisfy { $0 < $1 }, "\(measured)")
    }
}
