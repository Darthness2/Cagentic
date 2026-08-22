import Foundation
import SwiftUI
import UIKit

/// A small, native Markdown renderer tuned for chat transcripts.
///
/// Foundation's `AttributedString` handles inline Markdown while this view owns
/// block-level layout so code, lists, and quotations remain useful SwiftUI
/// controls instead of becoming one undifferentiated text run.
struct MarkdownText: View {
    let source: String

    @State private var renderedDocument: RenderedMarkdownDocument?
    @State private var pendingSource = ""
    @State private var parsingTask: Task<Void, Never>?
    @State private var parserGeneration = UUID()

    init(_ source: String) {
        self.source = source
    }

    var body: some View {
        renderedContent
            .onChange(of: source, initial: true) { _, newSource in
                enqueueForRendering(newSource)
            }
            .onDisappear {
                parserGeneration = UUID()
                parsingTask?.cancel()
                parsingTask = nil
            }
            .transaction { transaction in
                transaction.animation = nil
            }
    }

    @ViewBuilder
    private var renderedContent: some View {
        if let renderedDocument,
           source.hasPrefix(renderedDocument.source)
        {
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
                ForEach(renderedDocument.document.blocks) { block in
                    MarkdownBlockView(block: block)
                }

                let tail = String(source.dropFirst(renderedDocument.source.count))
                if !tail.isEmpty {
                    Text(tail)
                        .font(CagenticTheme.FontStyle.body)
                        .foregroundStyle(CagenticTheme.textPrimary)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        } else {
            plainText
        }
    }

    private func enqueueForRendering(_ newSource: String) {
        pendingSource = newSource
        guard parsingTask == nil else { return }

        let generation = UUID()
        parserGeneration = generation
        parsingTask = Task {
            while !Task.isCancelled {
                let candidate = pendingSource
                guard !candidate.isEmpty else {
                    renderedDocument = nil
                    break
                }

                let document = await MarkdownDocument.parseOffMain(candidate)
                guard !Task.isCancelled else { break }
                renderedDocument = RenderedMarkdownDocument(
                    source: candidate,
                    document: document
                )
                guard pendingSource != candidate else { break }
            }

            guard parserGeneration == generation else { return }
            parsingTask = nil
        }
    }

    private var plainText: some View {
        Text(source)
            .font(CagenticTheme.FontStyle.body)
            .foregroundStyle(CagenticTheme.textPrimary)
            .tint(CagenticTheme.accent)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private nonisolated struct RenderedMarkdownDocument: Equatable, Sendable {
    let source: String
    let document: MarkdownDocument
}

private struct MarkdownBlockView: View {
    let block: MarkdownBlock

    var body: some View {
        switch block.content {
        case let .paragraph(text):
            InlineMarkdownText(text, font: CagenticTheme.FontStyle.body)
                .lineSpacing(4)

        case let .heading(level, text):
            InlineMarkdownText(text, font: headingFont(for: level))
                .padding(.top, level == 1 ? CagenticTheme.Spacing.xs : 0)
                .accessibilityAddTraits(.isHeader)

        case let .list(items):
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                ForEach(items) { item in
                    MarkdownListRow(item: item)
                }
            }

        case let .table(table):
            MarkdownTableView(table: table)

        case let .math(equation):
            MarkdownMathBlock(equation: equation)

        case let .quote(text):
            HStack(alignment: .top, spacing: CagenticTheme.Spacing.sm) {
                Divider()
                    .overlay(CagenticTheme.border)
                    .frame(width: 3)
                    .accessibilityHidden(true)

                InlineMarkdownText(text, font: .inter(.body, italic: true))
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .lineSpacing(4)
            }
            .padding(.vertical, CagenticTheme.Spacing.xxs)
            .frame(maxWidth: .infinity, alignment: .leading)

        case let .code(language, code):
            MarkdownCodeBlock(language: language, code: code)
        }
    }

    private func headingFont(for level: Int) -> Font {
        switch level {
        case 1:
            CagenticTheme.FontStyle.title2
        case 2:
            CagenticTheme.FontStyle.heading
        default:
            CagenticTheme.FontStyle.headline
        }
    }
}

private struct InlineMarkdownText: View {
    private let font: Font
    private let renderedText: AttributedString

    init(_ inline: InlineMarkdown, font: Font) {
        self.font = font
        var attributed = inline.value
        Self.style(&attributed)
        renderedText = attributed
    }

    var body: some View {
        Text(renderedText)
            .font(font)
            .foregroundStyle(CagenticTheme.textPrimary)
            .tint(CagenticTheme.accent)
            .fixedSize(horizontal: false, vertical: true)
            .transaction { transaction in
                transaction.animation = nil
            }
    }

    private static func style(_ attributed: inout AttributedString) {
        let codeRanges = attributed.runs.compactMap { run in
            run.inlinePresentationIntent?.contains(.code) == true ? run.range : nil
        }
        let linkRanges = attributed.runs.compactMap { run in
            run.link == nil ? nil : run.range
        }

        for range in codeRanges {
            attributed[range].font = .system(.body, design: .monospaced)
            attributed[range].foregroundColor = CagenticTheme.textPrimary
            attributed[range].backgroundColor = CagenticTheme.surfaceRaised
        }
        for range in linkRanges {
            attributed[range].foregroundColor = CagenticTheme.accent
            attributed[range].underlineStyle = .single
        }
    }
}

private nonisolated struct InlineMarkdown: Equatable, Sendable {
    let source: String
    let value: AttributedString

    static func parse(_ source: String) -> InlineMarkdown {
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace
        )
        var value = AttributedString()
        for segment in mathSegments(in: source) {
            switch segment {
            case .markdown(let text):
                value += (try? AttributedString(markdown: text, options: options))
                    ?? AttributedString(text)
            case .math(let equation):
                var attributed = AttributedString(normalizedMath(equation))
                attributed.inlinePresentationIntent = .emphasized
                value += attributed
            }
        }
        return InlineMarkdown(source: source, value: value)
    }

    static func normalizedMath(_ source: String) -> String {
        let replacements = [
            "\\times": "×",
            "\\cdot": "·",
            "\\leq": "≤",
            "\\le": "≤",
            "\\geq": "≥",
            "\\ge": "≥",
            "\\neq": "≠",
            "\\infty": "∞",
            "\\sum": "∑",
            "\\prod": "∏",
            "\\sqrt": "√",
            "\\pi": "π",
            "\\theta": "θ",
            "\\Delta": "Δ",
            "\\rightarrow": "→",
            "\\leftarrow": "←",
        ]
        return replacements.reduce(source) { result, replacement in
            result.replacingOccurrences(of: replacement.key, with: replacement.value)
        }
    }

    private static func mathSegments(in source: String) -> [InlineSegment] {
        var result: [InlineSegment] = []
        var markdown = ""
        var index = source.startIndex
        var isInsideCode = false

        func flushMarkdown() {
            guard !markdown.isEmpty else { return }
            result.append(.markdown(markdown))
            markdown = ""
        }

        while index < source.endIndex {
            if source[index] == "`" {
                isInsideCode.toggle()
                markdown.append(source[index])
                index = source.index(after: index)
                continue
            }

            if !isInsideCode, source[index] == "$" {
                let contentStart = source.index(after: index)
                if let closing = source[contentStart...].firstIndex(of: "$"),
                   closing > contentStart,
                   isPlausibleDollarDelimitedMath(
                       in: source,
                       opening: index,
                       closing: closing
                   )
                {
                    flushMarkdown()
                    result.append(.math(String(source[contentStart..<closing])))
                    index = source.index(after: closing)
                    continue
                }
            }

            if !isInsideCode,
               source[index...].hasPrefix("\\("),
               let closing = source[index...].range(of: "\\)")
            {
                let contentStart = source.index(index, offsetBy: 2)
                guard closing.lowerBound > contentStart else {
                    markdown.append(source[index])
                    index = source.index(after: index)
                    continue
                }
                flushMarkdown()
                result.append(.math(String(source[contentStart..<closing.lowerBound])))
                index = closing.upperBound
                continue
            }

            markdown.append(source[index])
            index = source.index(after: index)
        }
        flushMarkdown()
        return result.isEmpty ? [.markdown(source)] : result
    }

    /// Avoid interpreting ordinary prices such as "$5 and $10" as equations.
    private static func isPlausibleDollarDelimitedMath(
        in source: String,
        opening: String.Index,
        closing: String.Index
    ) -> Bool {
        if opening > source.startIndex,
           source[source.index(before: opening)] == "\\"
        {
            return false
        }

        let afterClosing = source.index(after: closing)
        return afterClosing == source.endIndex || !source[afterClosing].isNumber
    }

    private enum InlineSegment {
        case markdown(String)
        case math(String)
    }
}

private struct MarkdownListRow: View {
    let item: MarkdownListItem

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: CagenticTheme.Spacing.xs) {
            Text(item.marker)
                .font(CagenticTheme.FontStyle.body.monospacedDigit())
                .foregroundStyle(CagenticTheme.textSecondary)
                .frame(minWidth: 20, alignment: .trailing)
                .accessibilityHidden(true)

            InlineMarkdownText(item.text, font: CagenticTheme.FontStyle.body)
                .lineSpacing(4)
        }
        .padding(.leading, CGFloat(item.depth) * CagenticTheme.Spacing.md)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            item.marker == "•" ? item.text.source : "\(item.marker) \(item.text.source)"
        )
    }
}

private struct MarkdownTableView: View {
    let table: MarkdownTable

    var body: some View {
        ScrollView(.horizontal) {
            Grid(horizontalSpacing: 0, verticalSpacing: 0) {
                tableRow(table.header, isHeader: true)
                ForEach(table.rows) { row in
                    tableRow(row, isHeader: false)
                }
            }
            .overlay {
                RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                    .stroke(CagenticTheme.border, lineWidth: 0.75)
            }
            .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
        }
        .scrollIndicators(.hidden)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Table with \(table.rows.count) rows")
    }

    private func tableRow(_ row: MarkdownTableRow, isHeader: Bool) -> some View {
        GridRow {
            ForEach(row.cells) { cell in
                InlineMarkdownText(
                    cell.text,
                    font: isHeader
                        ? CagenticTheme.FontStyle.captionMedium
                        : CagenticTheme.FontStyle.callout
                )
                .frame(
                    minWidth: 96,
                    maxWidth: 220,
                    minHeight: 44,
                    alignment: cell.alignment.frameAlignment
                )
                .padding(.horizontal, CagenticTheme.Spacing.sm)
                .background(isHeader ? CagenticTheme.surfaceRaised : CagenticTheme.surface)
                .overlay(alignment: .trailing) {
                    Divider().overlay(CagenticTheme.border.opacity(0.7))
                }
                .overlay(alignment: .bottom) {
                    Divider().overlay(CagenticTheme.border.opacity(0.7))
                }
            }
        }
    }
}

private struct MarkdownMathBlock: View {
    let equation: String

    var body: some View {
        ScrollView(.horizontal) {
            Text(InlineMarkdown.normalizedMath(equation))
                .font(.system(.body, design: .serif).italic())
                .foregroundStyle(CagenticTheme.textPrimary)
                .textSelection(.enabled)
                .padding(CagenticTheme.Spacing.md)
        }
        .scrollIndicators(.hidden)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(CagenticTheme.surfaceRaised)
        .overlay {
            RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                .stroke(CagenticTheme.border, lineWidth: 0.75)
        }
        .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Equation")
        .accessibilityValue(equation)
    }
}

private struct MarkdownCodeBlock: View {
    @State private var copied = false
    @Environment(\.cagenticHapticsEnabled) private var hapticsEnabled

    let language: String?
    let code: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: CagenticTheme.Spacing.xs) {
                Text(languageLabel)
                    .font(CagenticTheme.FontStyle.metadata)
                    .foregroundStyle(CagenticTheme.textSecondary)

                Spacer(minLength: CagenticTheme.Spacing.xs)

                Button(action: copyCode) {
                    Label(
                        copied ? "Copied" : "Copy",
                        systemImage: copied ? "checkmark" : "doc.on.doc"
                    )
                        .font(CagenticTheme.FontStyle.captionMedium)
                        .foregroundStyle(
                            copied ? CagenticTheme.success : CagenticTheme.textSecondary
                        )
                        .frame(minHeight: 44)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(copied ? "Code copied" : "Copy code")
            }
            .padding(.leading, CagenticTheme.Spacing.sm)
            .padding(.trailing, CagenticTheme.Spacing.xs)

            Divider()
                .overlay(CagenticTheme.border)

            ScrollView(.horizontal) {
                Text(code.isEmpty ? " " : code)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(CagenticTheme.textPrimary)
                    .textSelection(.enabled)
                    .padding(CagenticTheme.Spacing.sm)
            }
            .scrollIndicators(.hidden)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Code block")
            .accessibilityValue(code)
        }
        .background(CagenticTheme.surfaceRaised)
        .overlay {
            RoundedRectangle(cornerRadius: CagenticTheme.Radius.card)
                .stroke(CagenticTheme.border.opacity(0.9), lineWidth: 0.5)
        }
        .compositingGroup()
        .clipShape(.rect(cornerRadius: CagenticTheme.Radius.card))
        .sensoryFeedback(.success, trigger: copied) { oldValue, newValue in
            hapticsEnabled && !oldValue && newValue
        }
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(for: .seconds(1.6))
            guard !Task.isCancelled else { return }
            copied = false
        }
    }

    private var languageLabel: String {
        guard let language, !language.isEmpty else { return "CODE" }
        return language.uppercased()
    }

    private func copyCode() {
        UIPasteboard.general.string = code
        copied = true
        UIAccessibility.post(notification: .announcement, argument: "Code copied")
    }
}

private nonisolated struct MarkdownDocument: Equatable, Sendable {
    var blocks: [MarkdownBlock]

    @concurrent
    static func parseOffMain(_ source: String) async -> MarkdownDocument {
        parse(source)
    }

    static func parse(_ source: String) -> MarkdownDocument {
        let normalized = source
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        let lines = normalized
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init)
        var blocks: [MarkdownBlock] = []
        var index = 0

        func append(_ content: MarkdownBlock.Content) {
            blocks.append(MarkdownBlock(id: blocks.count, content: content))
        }

        while index < lines.count {
            let line = lines[index]
            if line.trimmingCharacters(in: .whitespaces).isEmpty {
                index += 1
                continue
            }

            if let fence = fenceInfo(in: line) {
                var codeLines: [String] = []
                index += 1
                while index < lines.count, !isClosingFence(lines[index], marker: fence.marker) {
                    codeLines.append(lines[index])
                    index += 1
                }
                if index < lines.count {
                    index += 1
                }
                append(.code(language: fence.language, code: codeLines.joined(separator: "\n")))
                continue
            }

            if let math = mathBlock(in: lines, startingAt: index) {
                append(.math(math.equation))
                index = math.nextIndex
                continue
            }

            if let heading = heading(in: line) {
                append(.heading(level: heading.level, text: InlineMarkdown.parse(heading.text)))
                index += 1
                continue
            }

            if let table = table(in: lines, startingAt: index) {
                append(.table(table.table))
                index = table.nextIndex
                continue
            }

            if quoteText(in: line) != nil {
                var quoteLines: [String] = []
                while index < lines.count, let text = quoteText(in: lines[index]) {
                    quoteLines.append(text)
                    index += 1
                }
                append(.quote(InlineMarkdown.parse(quoteLines.joined(separator: "\n"))))
                continue
            }

            if listItem(in: line) != nil {
                var items: [MarkdownListItem] = []
                while index < lines.count, let item = listItem(in: lines[index]) {
                    items.append(
                        MarkdownListItem(
                            id: items.count,
                            marker: item.marker,
                            depth: item.depth,
                            text: InlineMarkdown.parse(item.text)
                        )
                    )
                    index += 1
                }
                append(.list(items))
                continue
            }

            var paragraphLines: [String] = []
            while index < lines.count, !isBlockBoundary(lines, at: index) {
                paragraphLines.append(lines[index].trimmingCharacters(in: .whitespaces))
                index += 1
            }
            append(.paragraph(InlineMarkdown.parse(paragraphLines.joined(separator: " "))))
        }

        return MarkdownDocument(blocks: blocks)
    }

    private static func fenceInfo(in line: String) -> (marker: String, language: String?)? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let marker: String
        if trimmed.hasPrefix("```") {
            marker = "```"
        } else if trimmed.hasPrefix("~~~") {
            marker = "~~~"
        } else {
            return nil
        }

        let info = trimmed.dropFirst(marker.count).trimmingCharacters(in: .whitespaces)
        return (marker, info.isEmpty ? nil : info)
    }

    private static func isClosingFence(_ line: String, marker: String) -> Bool {
        line.trimmingCharacters(in: .whitespaces).hasPrefix(marker)
    }

    private static func heading(in line: String) -> (level: Int, text: String)? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let hashes = trimmed.prefix { $0 == "#" }.count
        guard (1...6).contains(hashes) else { return nil }
        let remainder = trimmed.dropFirst(hashes)
        guard remainder.first == " " else { return nil }
        return (hashes, remainder.dropFirst().trimmingCharacters(in: .whitespaces))
    }

    private static func quoteText(in line: String) -> String? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.first == ">" else { return nil }
        return trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
    }

    private static func listItem(in line: String) -> (marker: String, depth: Int, text: String)? {
        let indentation = line.prefix { $0 == " " || $0 == "\t" }.reduce(0) { count, character in
            count + (character == "\t" ? 4 : 1)
        }
        let depth = min(6, indentation / 2)
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        for marker in ["- ", "* ", "+ "] where trimmed.hasPrefix(marker) {
            return ("•", depth, String(trimmed.dropFirst(marker.count)))
        }
        let digits = trimmed.prefix { $0.isNumber }
        guard !digits.isEmpty, let ordinal = Int(digits) else { return nil }
        let remainder = trimmed.dropFirst(digits.count)
        let marker: String
        if remainder.hasPrefix(". ") {
            marker = ". "
        } else if remainder.hasPrefix(") ") {
            marker = ") "
        } else {
            return nil
        }
        return ("\(ordinal).", depth, String(remainder.dropFirst(marker.count)))
    }

    private static func mathBlock(
        in lines: [String],
        startingAt index: Int
    ) -> (equation: String, nextIndex: Int)? {
        let trimmed = lines[index].trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("$$"), trimmed.hasSuffix("$$"), trimmed.count > 4 {
            return (String(trimmed.dropFirst(2).dropLast(2)), index + 1)
        }
        let closingMarker: String
        if trimmed == "$$" {
            closingMarker = "$$"
        } else if trimmed == "\\[" {
            closingMarker = "\\]"
        } else {
            return nil
        }

        var equationLines: [String] = []
        var nextIndex = index + 1
        while nextIndex < lines.count {
            if lines[nextIndex].trimmingCharacters(in: .whitespaces) == closingMarker {
                return (equationLines.joined(separator: "\n"), nextIndex + 1)
            }
            equationLines.append(lines[nextIndex])
            nextIndex += 1
        }
        // Streaming output may not have received its closing delimiter yet. Render the partial
        // equation now instead of flashing back to plain text until the final token arrives.
        return (equationLines.joined(separator: "\n"), nextIndex)
    }

    private static func table(
        in lines: [String],
        startingAt index: Int
    ) -> (table: MarkdownTable, nextIndex: Int)? {
        guard index + 1 < lines.count,
              let headerCells = pipeCells(in: lines[index]),
              let separatorCells = pipeCells(in: lines[index + 1]),
              headerCells.count == separatorCells.count,
              !headerCells.isEmpty
        else {
            return nil
        }
        let alignments = separatorCells.map(tableAlignment)
        guard alignments.allSatisfy({ $0 != nil }) else { return nil }
        let resolvedAlignments = alignments.compactMap { $0 }

        func row(_ values: [String], id: Int) -> MarkdownTableRow {
            MarkdownTableRow(
                id: id,
                cells: headerCells.indices.map { column in
                    MarkdownTableCell(
                        id: column,
                        text: InlineMarkdown.parse(column < values.count ? values[column] : ""),
                        alignment: resolvedAlignments[column]
                    )
                }
            )
        }

        var rows: [MarkdownTableRow] = []
        var nextIndex = index + 2
        while nextIndex < lines.count,
              !lines[nextIndex].trimmingCharacters(in: .whitespaces).isEmpty,
              let values = pipeCells(in: lines[nextIndex])
        {
            rows.append(row(values, id: rows.count + 1))
            nextIndex += 1
        }
        return (
            MarkdownTable(header: row(headerCells, id: 0), rows: rows),
            nextIndex
        )
    }

    private static func pipeCells(in line: String) -> [String]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.contains("|") else { return nil }
        let withoutLeading = trimmed.hasPrefix("|") ? String(trimmed.dropFirst()) : trimmed
        let withoutEdges = withoutLeading.hasSuffix("|")
            ? String(withoutLeading.dropLast())
            : withoutLeading
        let cells = withoutEdges
            .split(separator: "|", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        return cells.isEmpty ? nil : cells
    }

    private static func tableAlignment(_ separator: String) -> MarkdownTableAlignment? {
        let trimmed = separator.trimmingCharacters(in: .whitespaces)
        let startsColon = trimmed.hasPrefix(":")
        let endsColon = trimmed.hasSuffix(":")
        let rule = trimmed.trimmingCharacters(in: CharacterSet(charactersIn: ":"))
        guard rule.count >= 3, rule.allSatisfy({ $0 == "-" }) else { return nil }
        if startsColon, endsColon { return .center }
        if endsColon { return .trailing }
        return .leading
    }

    private static func isBlockBoundary(_ lines: [String], at index: Int) -> Bool {
        let line = lines[index]
        if line.trimmingCharacters(in: .whitespaces).isEmpty {
            return true
        }
        return fenceInfo(in: line) != nil
            || mathBlock(in: lines, startingAt: index) != nil
            || heading(in: line) != nil
            || table(in: lines, startingAt: index) != nil
            || quoteText(in: line) != nil
            || listItem(in: line) != nil
    }
}

private nonisolated struct MarkdownBlock: Identifiable, Equatable, Sendable {
    nonisolated enum Content: Equatable, Sendable {
        case paragraph(InlineMarkdown)
        case heading(level: Int, text: InlineMarkdown)
        case list([MarkdownListItem])
        case table(MarkdownTable)
        case math(String)
        case quote(InlineMarkdown)
        case code(language: String?, code: String)
    }

    let id: Int
    let content: Content
}

private nonisolated struct MarkdownListItem: Identifiable, Equatable, Sendable {
    let id: Int
    let marker: String
    let depth: Int
    let text: InlineMarkdown
}

private nonisolated struct MarkdownTable: Equatable, Sendable {
    let header: MarkdownTableRow
    let rows: [MarkdownTableRow]
}

private nonisolated struct MarkdownTableRow: Identifiable, Equatable, Sendable {
    let id: Int
    let cells: [MarkdownTableCell]
}

private nonisolated struct MarkdownTableCell: Identifiable, Equatable, Sendable {
    let id: Int
    let text: InlineMarkdown
    let alignment: MarkdownTableAlignment
}

private nonisolated enum MarkdownTableAlignment: Equatable, Sendable {
    case leading
    case center
    case trailing

    var frameAlignment: Alignment {
        switch self {
        case .leading: .leading
        case .center: .center
        case .trailing: .trailing
        }
    }
}

/// A narrow test seam for the renderer's structural parser. Keeping the parsed view types private
/// prevents feature code from coupling to rendering internals while regression tests can still
/// protect tables, math blocks, and nested-list recognition.
nonisolated enum MarkdownFeatureInspector {
    static func inspect(_ source: String) -> MarkdownFeatureSummary {
        let document = MarkdownDocument.parse(source)
        var tableCount = 0
        var mathBlockCount = 0
        var maximumListDepth = 0
        for block in document.blocks {
            switch block.content {
            case .table:
                tableCount += 1
            case .math:
                mathBlockCount += 1
            case .list(let items):
                maximumListDepth = max(maximumListDepth, items.map(\.depth).max() ?? 0)
            default:
                break
            }
        }
        return MarkdownFeatureSummary(
            tableCount: tableCount,
            mathBlockCount: mathBlockCount,
            maximumListDepth: maximumListDepth
        )
    }
}

nonisolated struct MarkdownFeatureSummary: Equatable, Sendable {
    let tableCount: Int
    let mathBlockCount: Int
    let maximumListDepth: Int
}

#Preview("Markdown blocks") {
    ScrollView {
        MarkdownText(
            """
            # A useful answer

            Native **Markdown** supports *emphasis*, `inline code`, and [links](https://ollama.com).

            > Keep the interface quiet so the answer remains the focus.

            - Stream each response
              - Keep nested items aligned
                - Even while streaming
            - Preserve partial output

            1. Connect to the PC
            2. Select a model

            | Model | Vision | Context |
            |:------|:------:|--------:|
            | Gemma | Yes | 8,192 |
            | Qwen | No | 32,768 |

            Inline math keeps $E = mc^2$ readable.

            $$
            f(x) = \\sum_{i=1}^{n} x_i
            $$

            ```swift
            let response = try await client.chat(messages)
            ```
            """
        )
        .padding()
    }
    .background(CagenticTheme.stage)
}
