#!/usr/bin/env swift

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

private let canvasSize = 1024
private let designGrid: CGFloat = 24

private struct RGB {
    let red: UInt8
    let green: UInt8
    let blue: UInt8

    init(hex: UInt32) {
        red = UInt8((hex >> 16) & 0xFF)
        green = UInt8((hex >> 8) & 0xFF)
        blue = UInt8(hex & 0xFF)
    }

    func color(in colorSpace: CGColorSpace) -> CGColor {
        CGColor(
            colorSpace: colorSpace,
            components: [
                CGFloat(red) / 255,
                CGFloat(green) / 255,
                CGFloat(blue) / 255,
                1,
            ]
        )!
    }
}

private struct IconAppearance {
    let filename: String
    let background: RGB
    let mark: RGB
}

private let appearances = [
    IconAppearance(
        filename: "AppIcon-Standard.png",
        background: RGB(hex: 0xF7F9FB),
        mark: RGB(hex: 0x0969DA)
    ),
    IconAppearance(
        filename: "AppIcon-Dark.png",
        background: RGB(hex: 0x0A0C10),
        mark: RGB(hex: 0x7CC4FF)
    ),
    // iOS applies the user's selected tint to this luminance artwork. Keep it
    // deliberately neutral while retaining the light-background/dark-mark
    // contrast of Cagentic's graphite palette.
    IconAppearance(
        filename: "AppIcon-Tinted.png",
        background: RGB(hex: 0xF2F2F2),
        mark: RGB(hex: 0x242424)
    ),
]

private enum GenerationError: Error, CustomStringConvertible {
    case cannotCreateColorSpace
    case cannotCreateContext
    case cannotCreateImage
    case cannotCreateDestination(URL)
    case cannotWriteImage(URL)

    var description: String {
        switch self {
        case .cannotCreateColorSpace:
            "Could not create an sRGB color space."
        case .cannotCreateContext:
            "Could not create the 1024px bitmap context."
        case .cannotCreateImage:
            "Could not create an image from the bitmap context."
        case .cannotCreateDestination(let url):
            "Could not create a PNG destination at \(url.path)."
        case .cannotWriteImage(let url):
            "Could not finalize the PNG at \(url.path)."
        }
    }
}

/// The exact 24-point Cagentic `i-spark` geometry from the gateway and browser
/// extension. The source SVG lives beside this script for design-tool use.
private func makeSparkPath(scale: CGFloat) -> CGPath {
    func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        CGPoint(x: x * scale, y: y * scale)
    }

    let path = CGMutablePath()
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

private func render(_ appearance: IconAppearance, to outputURL: URL) throws {
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        throw GenerationError.cannotCreateColorSpace
    }

    let bitmapInfo = CGBitmapInfo.byteOrder32Big.rawValue
        | CGImageAlphaInfo.noneSkipLast.rawValue
    guard let context = CGContext(
        data: nil,
        width: canvasSize,
        height: canvasSize,
        bitsPerComponent: 8,
        bytesPerRow: canvasSize * 4,
        space: colorSpace,
        bitmapInfo: bitmapInfo
    ) else {
        throw GenerationError.cannotCreateContext
    }

    context.setFillColor(appearance.background.color(in: colorSpace))
    context.fill(CGRect(x: 0, y: 0, width: canvasSize, height: canvasSize))

    let scale = CGFloat(canvasSize) / designGrid
    context.addPath(makeSparkPath(scale: scale))
    context.setStrokeColor(appearance.mark.color(in: colorSpace))
    context.setLineWidth(1.7 * scale)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)
    context.strokePath()

    guard let image = context.makeImage() else {
        throw GenerationError.cannotCreateImage
    }
    guard let destination = CGImageDestinationCreateWithURL(
        outputURL as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        throw GenerationError.cannotCreateDestination(outputURL)
    }

    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        throw GenerationError.cannotWriteImage(outputURL)
    }
}

private func main() throws {
    let scriptURL = URL(fileURLWithPath: CommandLine.arguments[0])
        .standardizedFileURL
    let assetDirectory = scriptURL
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("Cagentic/Resources/Assets.xcassets/AppIcon.appiconset")

    try FileManager.default.createDirectory(
        at: assetDirectory,
        withIntermediateDirectories: true
    )

    for appearance in appearances {
        let outputURL = assetDirectory.appendingPathComponent(appearance.filename)
        try render(appearance, to: outputURL)
        print("Wrote \(outputURL.path)")
    }
}

do {
    try main()
} catch {
    FileHandle.standardError.write(Data("App icon generation failed: \(error)\n".utf8))
    exit(EXIT_FAILURE)
}
