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

    enum FontStyle {
        static let display = Font.inter(.largeTitle, weight: .bold)
        static let displaySmall = Font.inter(.title2, weight: .bold)
        static let title = Font.inter(.title, weight: .bold)
        static let title2 = Font.inter(.title2, weight: .bold)
        static let title3 = Font.inter(.title3)
        static let heading = Font.inter(.title3, weight: .semibold)
        static let headline = Font.inter(.headline, weight: .semibold)
        static let body = Font.inter(.body)
        static let bodyMedium = Font.inter(.body, weight: .medium)
        static let bodySemibold = Font.inter(.body, weight: .semibold)
        static let callout = Font.inter(.callout)
        static let calloutMedium = Font.inter(.callout, weight: .medium)
        static let subheadline = Font.inter(.subheadline)
        static let subheadlineMedium = Font.inter(.subheadline, weight: .medium)
        static let subheadlineSemibold = Font.inter(.subheadline, weight: .semibold)
        static let caption = Font.inter(.caption)
        static let captionMedium = Font.inter(.caption, weight: .medium)
        static let captionSemibold = Font.inter(.caption, weight: .semibold)
        static let captionBold = Font.inter(.caption, weight: .bold)
        static let caption2 = Font.inter(.caption2)
        static let caption2Bold = Font.inter(.caption2, weight: .bold)
        static let footnote = Font.inter(.footnote)
        static let metadata = Font.inter(.caption).monospacedDigit()
    }

    @MainActor
    static func configureUIKitTypography() {
        let navigationBar = UINavigationBar.appearance()
        var titleAttributes = navigationBar.titleTextAttributes ?? [:]
        titleAttributes[.font] = UIFont.inter(
            textStyle: .headline,
            weight: .semibold
        )
        navigationBar.titleTextAttributes = titleAttributes

        var largeTitleAttributes = navigationBar.largeTitleTextAttributes ?? [:]
        largeTitleAttributes[.font] = UIFont.inter(
            textStyle: .largeTitle,
            weight: .bold
        )
        navigationBar.largeTitleTextAttributes = largeTitleAttributes

        let barButton = UIBarButtonItem.appearance()
        let barButtonAttributes: [NSAttributedString.Key: Any] = [
            .font: UIFont.inter(textStyle: .body)
        ]
        barButton.setTitleTextAttributes(barButtonAttributes, for: .normal)
        barButton.setTitleTextAttributes(barButtonAttributes, for: .highlighted)

        UITextField.appearance(whenContainedInInstancesOf: [UISearchBar.self]).font = UIFont.inter(
            textStyle: .body
        )
    }
}

enum CagenticFontWeight {
    case regular
    case medium
    case semibold
    case bold

    fileprivate var postScriptSuffix: String {
        switch self {
        case .regular: ""
        case .medium: "-Medium"
        case .semibold: "-SemiBold"
        case .bold: "-Bold"
        }
    }
}

extension Font {
    static func inter(
        _ textStyle: Font.TextStyle,
        weight: CagenticFontWeight = .regular,
        italic: Bool = false
    ) -> Font {
        let family = italic ? "InterVariableItalic" : "InterVariable"
        return .custom(
            family + weight.postScriptSuffix,
            size: interPointSize(for: textStyle),
            relativeTo: textStyle
        )
    }

    private static func interPointSize(for textStyle: Font.TextStyle) -> CGFloat {
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

extension UIFont {
    fileprivate static func inter(
        textStyle: UIFont.TextStyle,
        weight: CagenticFontWeight = .regular
    ) -> UIFont {
        let baseSize = preferredFont(forTextStyle: textStyle).pointSize
        let name = "InterVariable" + weight.postScriptSuffix
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
