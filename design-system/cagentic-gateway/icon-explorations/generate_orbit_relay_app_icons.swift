#!/usr/bin/env swift

// Renders approved Orbit Relay app-icon deliverables without changing the
// product asset catalog. Outputs live beside this script under
// `final-orbit-relay-app-icons/` for deliberate adoption later.

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
        filename: "Cagentic-OrbitRelay-Standard.png",
        background: RGB(hex: 0xF7F9FB),
        mark: RGB(hex: 0x0969DA)
    ),
    IconAppearance(
        filename: "Cagentic-OrbitRelay-Dark.png",
        background: RGB(hex: 0x0A0C10),
        mark: RGB(hex: 0x7CC4FF)
    ),
    // iOS applies the user's Home Screen tint to this luminance artwork.
    IconAppearance(
        filename: "Cagentic-OrbitRelay-Tinted.png",
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
            "Could not create a 1024px bitmap context."
        case .cannotCreateImage:
            "Could not create an image from the bitmap context."
        case let .cannotCreateDestination(url):
            "Could not create image destination: \(url.path)"
        case let .cannotWriteImage(url):
            "Could not finalize image: \(url.path)"
        }
    }
}

private func scaledPoint(_ x: CGFloat, _ y: CGFloat, scale: CGFloat) -> CGPoint {
    CGPoint(x: x * scale, y: y * scale)
}

private func makeOrbitRelayArcs(scale: CGFloat) -> CGPath {
    let path = CGMutablePath()
    let center = scaledPoint(12, 12, scale: scale)
    let radius: CGFloat = 8.45 * scale
    let startAngles: [CGFloat] = [-.pi / 2, 0, .pi / 2, .pi]
    let arcLength: CGFloat = .pi / 3

    for startAngle in startAngles {
        path.move(
            to: CGPoint(
                x: center.x + radius * cos(startAngle),
                y: center.y + radius * sin(startAngle)
            )
        )
        path.addArc(
            center: center,
            radius: radius,
            startAngle: startAngle,
            endAngle: startAngle + arcLength,
            clockwise: false
        )
    }
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
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)
    context.addPath(makeOrbitRelayArcs(scale: scale))
    context.setStrokeColor(appearance.mark.color(in: colorSpace))
    context.setLineWidth(2.1 * scale)
    context.setLineCap(.round)
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
    let scriptURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
    let outputDirectory = scriptURL
        .deletingLastPathComponent()
        .appendingPathComponent("final-orbit-relay-app-icons")

    try FileManager.default.createDirectory(
        at: outputDirectory,
        withIntermediateDirectories: true
    )

    for appearance in appearances {
        let outputURL = outputDirectory.appendingPathComponent(appearance.filename)
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
