import SwiftUI
import UIKit

/// Pairs this device with a Cagentic gateway on the same network.
///
/// The whole point is that nothing here has to be typed except six digits: the
/// computer announces itself, and the code buys the token. Manual address entry
/// still exists behind `ConnectionSetupView` for networks where discovery is
/// blocked, but it is no longer the way in.
struct GatewayPairingView: View {
    @Bindable var model: AppModel
    @Environment(\.dismiss) private var dismiss

    @State private var discovery = GatewayDiscovery()
    @State private var selected: DiscoveredGateway?
    @State private var code = ""
    @State private var isPairing = false
    @State private var inlineError: String?
    @FocusState private var isCodeFocused: Bool
    @AccessibilityFocusState private var isErrorFocused: Bool

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CagenticTheme.Spacing.lg) {
                    introduction
                    machines
                    if selected != nil {
                        codeEntry
                    }
                    instructions
                }
                .frame(maxWidth: 620, alignment: .leading)
                .padding(CagenticTheme.Spacing.lg)
                .frame(maxWidth: .infinity)
            }
            .background(CagenticTheme.background)
            .navigationTitle("Add a computer")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel", role: .cancel) { dismiss() }
                        .disabled(isPairing)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if isPairing {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Connect") { pair() }
                            .fontWeight(.semibold)
                            .disabled(!canPair)
                    }
                }
            }
            .interactiveDismissDisabled(isPairing)
        }
        .onAppear { discovery.start() }
        .onDisappear { discovery.stop() }
    }

    private var introduction: some View {
        HStack(alignment: .top, spacing: CagenticTheme.Spacing.md) {
            BrandMark(size: 38)
            VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
                Text("Connect to Cagentic on your computer")
                    .font(CagenticTheme.FontStyle.displaySmall)
                    .foregroundStyle(CagenticTheme.textPrimary)
                Text("Run /token on the computer, pick it below, and enter the six digits it shows.")
                    .font(CagenticTheme.FontStyle.body)
                    .foregroundStyle(CagenticTheme.textSecondary)
            }
        }
    }

    @ViewBuilder
    private var machines: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
            HStack(spacing: CagenticTheme.Spacing.xs) {
                Text("On your Wi-Fi")
                    .font(CagenticTheme.FontStyle.heading)
                if discovery.isSearching, discovery.gateways.isEmpty {
                    ProgressView().controlSize(.mini)
                }
            }

            if let failure = discovery.failure {
                Label(failure, systemImage: "wifi.exclamationmark")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.warning)
                    .fixedSize(horizontal: false, vertical: true)
            } else if discovery.gateways.isEmpty {
                Text("Looking for computers running Cagentic. They appear here once the gateway is running with network access turned on.")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(spacing: 0) {
                    ForEach(discovery.gateways) { gateway in
                        machineRow(gateway)
                        if gateway.id != discovery.gateways.last?.id {
                            Divider().overlay(CagenticTheme.border)
                        }
                    }
                }
            }
        }
        .cagenticCard()
    }

    private func machineRow(_ gateway: DiscoveredGateway) -> some View {
        Button {
            guard !isPairing else { return }
            selected = gateway
            inlineError = nil
            isCodeFocused = true
        } label: {
            HStack(spacing: CagenticTheme.Spacing.sm) {
                Image(systemName: "desktopcomputer")
                    .foregroundStyle(
                        selected == gateway ? CagenticTheme.accent : CagenticTheme.textSecondary
                    )
                    .frame(width: 28)

                VStack(alignment: .leading, spacing: 2) {
                    Text(gateway.name)
                        .font(CagenticTheme.FontStyle.bodyMedium)
                        .foregroundStyle(CagenticTheme.textPrimary)
                        .lineLimit(1)
                    if let version = gateway.version, !version.isEmpty {
                        Text("Cagentic \(version)")
                            .font(CagenticTheme.FontStyle.caption)
                            .foregroundStyle(CagenticTheme.textTertiary)
                    }
                }

                Spacer(minLength: 0)

                if selected == gateway {
                    Image(systemName: "checkmark")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(CagenticTheme.accent)
                }
            }
            .frame(minHeight: 44)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected == gateway ? .isSelected : [])
    }

    private var codeEntry: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.xs) {
            Text("Pairing code")
                .font(CagenticTheme.FontStyle.subheadlineSemibold)

            TextField("000000", text: $code)
                .font(CagenticTheme.FontStyle.Code.entry)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .focused($isCodeFocused)
                .padding(CagenticTheme.Spacing.sm)
                .background(
                    CagenticTheme.background,
                    in: .rect(cornerRadius: CagenticTheme.Radius.control)
                )
                .accessibilityLabel("Pairing code")
                .onChange(of: code) { _, entered in
                    // Six digits, and nothing else: the field should be
                    // impossible to get wrong.
                    let digits = String(entered.filter(\.isNumber).prefix(6))
                    if digits != entered {
                        code = digits
                    }
                    inlineError = nil
                }

            Text("The code expires a couple of minutes after /token shows it. Run it again for a fresh one.")
                .font(CagenticTheme.FontStyle.caption)
                .foregroundStyle(CagenticTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            if let inlineError {
                Label(inlineError, systemImage: "exclamationmark.triangle.fill")
                    .font(CagenticTheme.FontStyle.callout)
                    .foregroundStyle(CagenticTheme.error)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityLabel("Pairing error: \(inlineError)")
                    .accessibilityFocused($isErrorFocused)
            }
        }
        .cagenticCard()
    }

    private var instructions: some View {
        VStack(alignment: .leading, spacing: CagenticTheme.Spacing.sm) {
            Text("On the computer")
                .font(CagenticTheme.FontStyle.heading)
            Text("Type /token in Cagentic — in the terminal or on the web page. It turns on network access if it is off, then shows a code.")
                .font(CagenticTheme.FontStyle.callout)
                .foregroundStyle(CagenticTheme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)

            Button("Enter an address instead") {
                model.queueSheetAfterDismiss(.connection)
                dismiss()
            }
            .font(CagenticTheme.FontStyle.callout)
            .disabled(isPairing)
        }
        .cagenticCard()
    }

    private var canPair: Bool {
        selected != nil && code.count == 6 && !isPairing
    }

    private func pair() {
        guard let selected, canPair else { return }
        isPairing = true
        inlineError = nil
        isErrorFocused = false
        Task {
            let result = await model.pairGateway(
                with: selected,
                code: code,
                discovery: discovery
            )
            isPairing = false
            switch result {
            case .success:
                UIAccessibility.post(
                    notification: .announcement,
                    argument: "Connected to \(selected.name)."
                )
                dismiss()
            case .failure(let error):
                guard !(error is CancellationError) else { return }
                inlineError = Self.message(for: error)
                isErrorFocused = true
                code = ""
                isCodeFocused = true
            }
        }
    }

    private static func message(for error: Error) -> String {
        if let gatewayError = error as? GatewayClientError {
            return [gatewayError.errorDescription, gatewayError.recoverySuggestion]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
                .joined(separator: " ")
        }
        return error.localizedDescription
    }
}

#Preview("Pair a gateway") {
    GatewayPairingView(model: .preview(disconnected: true))
}
