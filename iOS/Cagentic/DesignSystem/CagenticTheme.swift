import SwiftUI

private struct CagenticHapticsEnabledKey: EnvironmentKey {
    static let defaultValue = true
}

extension EnvironmentValues {
    var cagenticHapticsEnabled: Bool {
        get { self[CagenticHapticsEnabledKey.self] }
        set { self[CagenticHapticsEnabledKey.self] = newValue }
    }
}

enum CagenticTheme {
    // Keep these semantic roles in step with cagentic/gateway_assets/app.css.
    // SwiftUI uses opaque equivalents for the gateway's translucent layers so
    // contrast remains predictable across native sheets and navigation bars.
    static let accent = Color(light: "0969DA", dark: "7CC4FF")
    static let accentSoft = Color(light: "EAF2FB", dark: "132636")
    static let accentPressed = Color(light: "075ABD", dark: "9BD2FF")
    static let onAccent = Color(light: "FFFFFF", dark: "06101A")

    static let background = Color(light: "F7F9FB", dark: "0A0C10")
    static let stage = Color(light: "FFFFFF", dark: "06080B")
    static let surface = Color(light: "FFFFFF", dark: "10141A")
    static let surfaceRaised = Color(light: "F0F4F8", dark: "14191F")

    static let textPrimary = Color(light: "111820", dark: "E7EDF4")
    static let textSecondary = Color(light: "46515D", dark: "B9C2CE")
    static let textTertiary = Color(light: "596675", dark: "8F9BAA")
    static let border = Color(light: "D0D7DE", dark: "27313C")

    static let success = Color(light: "2F7D43", dark: "8ECF95")
    static let warning = Color(light: "8A6D10", dark: "D9C069")
    static let error = Color(light: "B03A35", dark: "D98A87")

    enum Spacing {
        static let xxs: CGFloat = 4
        static let xs: CGFloat = 8
        static let sm: CGFloat = 12
        static let md: CGFloat = 16
        static let lg: CGFloat = 24
        static let xl: CGFloat = 32
        static let xxl: CGFloat = 48
    }

    enum Radius {
        static let control: CGFloat = 8
        static let card: CGFloat = 12
        static let sheet: CGFloat = 16
        static let composer: CGFloat = 26
    }

    /// Chrome type: navigation, settings, buttons, labels — everything that is
    /// the app talking rather than the assistant. Space Grotesk.
    enum FontStyle {
        static let display = Font.cagentic(.ui, .largeTitle, weight: .bold)
        static let displaySmall = Font.cagentic(.ui, .title2, weight: .bold)
        static let title = Font.cagentic(.ui, .title, weight: .bold)
        static let title2 = Font.cagentic(.ui, .title2, weight: .bold)
        static let title3 = Font.cagentic(.ui, .title3)
        static let heading = Font.cagentic(.ui, .title3, weight: .semibold)
        static let headline = Font.cagentic(.ui, .headline, weight: .semibold)
        static let body = Font.cagentic(.ui, .body)
        static let bodyMedium = Font.cagentic(.ui, .body, weight: .medium)
        static let bodySemibold = Font.cagentic(.ui, .body, weight: .semibold)
        static let callout = Font.cagentic(.ui, .callout)
        static let calloutMedium = Font.cagentic(.ui, .callout, weight: .medium)
        static let subheadline = Font.cagentic(.ui, .subheadline)
        static let subheadlineMedium = Font.cagentic(.ui, .subheadline, weight: .medium)
        static let subheadlineSemibold = Font.cagentic(.ui, .subheadline, weight: .semibold)
        static let caption = Font.cagentic(.ui, .caption)
        static let captionMedium = Font.cagentic(.ui, .caption, weight: .medium)
        static let captionSemibold = Font.cagentic(.ui, .caption, weight: .semibold)
        static let captionBold = Font.cagentic(.ui, .caption, weight: .bold)
        static let caption2 = Font.cagentic(.ui, .caption2)
        static let caption2Bold = Font.cagentic(.ui, .caption2, weight: .bold)
        static let footnote = Font.cagentic(.ui, .footnote)
        static let metadata = Font.cagentic(.ui, .caption).monospacedDigit()

        /// Conversation type: what the user wrote and what the assistant said,
        /// including the headings and lists inside an answer. Plus Jakarta Sans,
        /// which is drawn for reading at length rather than for labels.
        enum Message {
            static let body = Font.cagentic(.reading, .body)
            static let bodyItalic = Font.cagentic(.reading, .body, italic: true)
            static let bodySemibold = Font.cagentic(.reading, .body, weight: .semibold)
            static let callout = Font.cagentic(.reading, .callout)
            static let captionMedium = Font.cagentic(.reading, .caption, weight: .medium)
            /// Markdown heading levels, largest first.
            static let heading1 = Font.cagentic(.reading, .title2, weight: .bold)
            static let heading2 = Font.cagentic(.reading, .title3, weight: .semibold)
            static let heading3 = Font.cagentic(.reading, .headline, weight: .semibold)
        }

        /// Code, command output, and anything else that is a payload rather
        /// than prose. JetBrains Mono.
        enum Code {
            /// A `code span` inside a sentence.
            static let inline = Font.cagentic(.code, .body)
            /// Fenced code, command output, attached text files.
            static let block = Font.cagentic(.code, .callout)
            /// Dense payloads shown inside a control, such as a permission diff.
            static let detail = Font.cagentic(.code, .caption2)
            /// A field the user types a fixed-width value into.
            static let entry = Font.cagentic(.code, .title2)
        }
    }

    @MainActor
    static func configureUIKitTypography() {
        let navigationBar = UINavigationBar.appearance()
        var titleAttributes = navigationBar.titleTextAttributes ?? [:]
        titleAttributes[.font] = UIFont.cagentic(
            .ui,
            textStyle: .headline,
            weight: .semibold
        )
        navigationBar.titleTextAttributes = titleAttributes

        var largeTitleAttributes = navigationBar.largeTitleTextAttributes ?? [:]
        largeTitleAttributes[.font] = UIFont.cagentic(
            .ui,
            textStyle: .largeTitle,
            weight: .bold
        )
        navigationBar.largeTitleTextAttributes = largeTitleAttributes

        let barButton = UIBarButtonItem.appearance()
        let barButtonAttributes: [NSAttributedString.Key: Any] = [
            .font: UIFont.cagentic(.ui, textStyle: .body)
        ]
        barButton.setTitleTextAttributes(barButtonAttributes, for: .normal)
        barButton.setTitleTextAttributes(barButtonAttributes, for: .highlighted)

        UITextField.appearance(whenContainedInInstancesOf: [UISearchBar.self]).font = UIFont.cagentic(
            .ui,
            textStyle: .body
        )
    }
}

/// The three faces the app ships. Splitting them by role rather than by name
/// keeps the choice at the call site about *what the text is*, so a family swap
/// is one line here instead of a sweep through every view.
enum CagenticFontFamily: CaseIterable {
    /// Space Grotesk — chrome. Its wide, slightly geometric shapes read well as
    /// labels and titles and badly as paragraphs, which is exactly the split.
    case ui
    /// Plus Jakarta Sans — conversation text, where line length is long and the
    /// eye needs a humanist face.
    case reading
    /// JetBrains Mono — code and command output.
    case code

    fileprivate var postScriptFamily: String {
        switch self {
        case .ui: "SpaceGrotesk"
        case .reading: "PlusJakartaSans"
        case .code: "JetBrainsMono"
        }
    }

    /// Only the roman weights each family actually ships in the bundle. Asking
    /// for one that is not registered silently falls back to the system face,
    /// so the theme is deliberately limited to what `UIAppFonts` lists.
    fileprivate func resolved(_ weight: CagenticFontWeight, italic: Bool) -> CagenticFontWeight {
        switch self {
        // Only the regular italic is bundled: emphasis in an answer is common,
        // bold emphasis inside it is not, and SwiftUI slants the semibold face
        // acceptably for the rare case.
        case .reading where italic: .regular
        // Code is set at one weight; there is no bold monospace in the UI.
        case .code: .regular
        case .ui, .reading: weight
        }
    }

    fileprivate var hasItalic: Bool { self == .reading }
}

enum CagenticFontWeight: CaseIterable {
    case regular
    case medium
    case semibold
    case bold

    fileprivate var postScriptSuffix: String {
        switch self {
        case .regular: "-Regular"
        case .medium: "-Medium"
        case .semibold: "-SemiBold"
        case .bold: "-Bold"
        }
    }
}

extension Font {
    static func cagentic(
        _ family: CagenticFontFamily,
        _ textStyle: Font.TextStyle,
        weight: CagenticFontWeight = .regular,
        italic: Bool = false
    ) -> Font {
        .custom(
            CagenticFontName.postScript(family, weight: weight, italic: italic),
            size: cagenticPointSize(for: textStyle),
            relativeTo: textStyle
        )
    }

    /// The point sizes the type scale is anchored to. `relativeTo:` then hands
    /// Dynamic Type back the scaling, so these are the sizes at the default
    /// content size rather than fixed ones.
    private static func cagenticPointSize(for textStyle: Font.TextStyle) -> CGFloat {
        switch textStyle {
        case .largeTitle: 34
        case .title: 28
        case .title2: 22
        case .title3: 20
        case .headline, .body: 17
        case .callout: 16
        case .subheadline: 15
        case .footnote: 13
        case .caption: 12
        case .caption2: 11
        default: 17
        }
    }
}

/// Resolves a role to the PostScript name of a face that is actually bundled.
/// Shared with the tests, which assert the name against `UIAppFonts` — a
/// typo here is invisible at runtime because the system font quietly stands in.
enum CagenticFontName {
    static func postScript(
        _ family: CagenticFontFamily,
        weight: CagenticFontWeight = .regular,
        italic: Bool = false
    ) -> String {
        let wantsItalic = italic && family.hasItalic
        let resolved = family.resolved(weight, italic: italic)
        // The italic face is the regular one; there is no PlusJakartaSans-RegularItalic.
        let suffix = wantsItalic ? "-Italic" : resolved.postScriptSuffix
        return family.postScriptFamily + suffix
    }
}

extension UIFont {
    fileprivate static func cagentic(
        _ family: CagenticFontFamily,
        textStyle: UIFont.TextStyle,
        weight: CagenticFontWeight = .regular
    ) -> UIFont {
        let baseSize = preferredFont(forTextStyle: textStyle).pointSize
        let name = CagenticFontName.postScript(family, weight: weight)
        let baseFont = UIFont(name: name, size: baseSize) ?? preferredFont(forTextStyle: textStyle)
        return UIFontMetrics(forTextStyle: textStyle).scaledFont(for: baseFont)
    }
}

extension Color {
    nonisolated init(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)

        let alpha: UInt64
        let red: UInt64
        let green: UInt64
        let blue: UInt64
        switch cleaned.count {
        case 3:
            (alpha, red, green, blue) = (
                255,
                (value >> 8) * 17,
                ((value >> 4) & 0xF) * 17,
                (value & 0xF) * 17
            )
        case 8:
            (alpha, red, green, blue) = (
                value >> 24,
                (value >> 16) & 0xFF,
                (value >> 8) & 0xFF,
                value & 0xFF
            )
        default:
            (alpha, red, green, blue) = (
                255,
                value >> 16,
                (value >> 8) & 0xFF,
                value & 0xFF
            )
        }

        self.init(
            .sRGB,
            red: Double(red) / 255,
            green: Double(green) / 255,
            blue: Double(blue) / 255,
            opacity: Double(alpha) / 255
        )
    }

    nonisolated init(light lightHex: String, dark darkHex: String) {
        self.init(
            uiColor: UIColor { traits in
                UIColor(Color(hex: traits.userInterfaceStyle == .dark ? darkHex : lightHex))
            }
        )
    }
}

extension AppearancePreference {
    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

extension View {
    /// Liquid Glass where the system provides it, with a quiet material fallback.
    ///
    /// The app ships back to iOS 18, where `glassEffect` does not exist; the fallback keeps the
    /// same floating-control shape using the material that was available then.
    @ViewBuilder
    func cagenticGlass<S: Shape>(in shape: S = Capsule()) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(.regular, in: shape)
        } else {
            background(.ultraThinMaterial, in: shape)
                .overlay {
                    shape.stroke(CagenticTheme.border.opacity(0.6), lineWidth: 0.5)
                }
        }
    }
}

extension View {
    /// Removes the system's top scroll-edge treatment.
    ///
    /// Its default draws the hard divider the floating controls exist to avoid, and its soft
    /// variant fades too little to keep the transcript off the clock. The chat screen paints its
    /// own top scrim instead, matching the one under the composer.
    @ViewBuilder
    func cagenticHidesTopScrollEdge() -> some View {
        if #available(iOS 26.0, *) {
            scrollEdgeEffectHidden(true, for: .top)
        } else {
            self
        }
    }
}

struct CagenticCardModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(CagenticTheme.Spacing.md)
            .background(CagenticTheme.surface)
            .overlay {
                RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                    .stroke(CagenticTheme.border.opacity(0.7), lineWidth: 0.5)
            }
            .compositingGroup()
            .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
    }
}

extension View {
    func cagenticCard() -> some View {
        modifier(CagenticCardModifier())
    }
}
