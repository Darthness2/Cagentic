import SwiftUI

struct RootView: View {
    @Bindable var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var isSidebarPresented = false
    @State private var drawerDragOffset: CGFloat = 0

    var body: some View {
        GeometryReader { proxy in
            let drawerWidth = min(max(proxy.size.width * 0.84, 280), 340)
            let drawerOffset = sidebarOffset(drawerWidth: drawerWidth)
            let revealProgress = sidebarRevealProgress(
                drawerWidth: drawerWidth,
                drawerOffset: drawerOffset
            )

            ZStack(alignment: .leading) {
                mainNavigation
                    .allowsHitTesting(!isSidebarPresented)
                    .accessibilityHidden(isSidebarPresented)

                if isSidebarPresented || drawerDragOffset > 0 {
                    Button(action: dismissSidebar) {
                        Color.black.opacity(0.22 * revealProgress)
                            .ignoresSafeArea()
                            .contentShape(.rect)
                    }
                    .frame(width: max(proxy.size.width - drawerWidth, 0))
                    .frame(maxHeight: .infinity)
                    .offset(x: drawerWidth)
                    .buttonStyle(.plain)
                    .allowsHitTesting(isSidebarPresented)
                    .accessibilityLabel("Close sidebar")
                }

                sidebar(drawerWidth: drawerWidth)
                    .offset(x: drawerOffset)
                    .shadow(
                        color: .black.opacity(0.2 * revealProgress),
                        radius: 20,
                        x: 8
                    )
                    .allowsHitTesting(isSidebarPresented)
                    .accessibilityHidden(!isSidebarPresented)
            }
            .clipped()
            .background(CagenticTheme.stage.ignoresSafeArea())
            .simultaneousGesture(openSidebarGesture(drawerWidth: drawerWidth))
        }
        // Span the whole screen so the clip lands on the device edges rather than on the safe-area
        // boundary. The navigation stack and the composer still inset themselves normally.
        .ignoresSafeArea()
        .accessibilityHidden(isOnboardingVisible)
        .environment(\.cagenticHapticsEnabled, model.settings.hapticsEnabled)
        .sheet(item: $model.presentedSheet, onDismiss: model.presentQueuedSheetIfNeeded) { destination in
            sheet(for: destination)
        }
        .fullScreenCover(isPresented: onboardingPresentation) {
            OnboardingView(model: model)
        }
        .alert(
            model.notice?.title ?? "Cagentic",
            isPresented: $model.isNoticePresented
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(model.notice?.message ?? "")
        }
    }

    private var mainNavigation: some View {
        NavigationStack {
            detail
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Open sidebar", systemImage: "sidebar.left") {
                            presentSidebar()
                        }
                        .accessibilityIdentifier("OpenSidebarButton")
                    }
                }
                // No bar strip: the three controls float over the transcript on their own glass,
                // and the content passes behind them.
                .toolbarBackground(.hidden, for: .navigationBar)
        }
        .background(CagenticTheme.stage.ignoresSafeArea())
    }

    private func sidebar(drawerWidth: CGFloat) -> some View {
        NavigationStack {
            SidebarView(model: model, isPresented: $isSidebarPresented)
        }
        .frame(width: drawerWidth)
        .frame(maxHeight: .infinity)
        .background {
            CagenticTheme.background
                .ignoresSafeArea()
        }
        .accessibilityAction(.escape, dismissSidebar)
        .overlay(alignment: .trailing) {
            // Keep the drawer-dismiss gesture on its edge. A gesture attached to the
            // whole sidebar competes with each conversation's trailing delete swipe.
            Color.clear
                .frame(width: 28)
                .contentShape(.rect)
                .gesture(closeSidebarGesture(drawerWidth: drawerWidth))
                .accessibilityHidden(true)
        }
    }

    private func openSidebarGesture(drawerWidth: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 18)
            .onChanged { value in
                guard !isSidebarPresented,
                      value.startLocation.x <= 28,
                      value.translation.width > 0,
                      abs(value.translation.width) > abs(value.translation.height)
                else {
                    return
                }
                drawerDragOffset = min(value.translation.width, drawerWidth)
            }
            .onEnded { value in
                guard !isSidebarPresented,
                      value.startLocation.x <= 28,
                      value.translation.width > 0,
                      abs(value.translation.width) > abs(value.translation.height)
                else {
                    settleDrawerDrag()
                    return
                }
                let projectedWidth = max(
                    value.translation.width,
                    value.predictedEndTranslation.width
                )
                if projectedWidth >= min(96, drawerWidth * 0.3) {
                    presentSidebar()
                } else {
                    settleDrawerDrag()
                }
            }
    }

    private func closeSidebarGesture(drawerWidth: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 18)
            .onChanged { value in
                guard isSidebarPresented,
                      value.translation.width < 0,
                      abs(value.translation.width) > abs(value.translation.height)
                else {
                    return
                }
                drawerDragOffset = max(value.translation.width, -drawerWidth)
            }
            .onEnded { value in
                guard isSidebarPresented,
                      value.translation.width < 0,
                      abs(value.translation.width) > abs(value.translation.height)
                else {
                    settleDrawerDrag()
                    return
                }
                let projectedWidth = min(
                    value.translation.width,
                    value.predictedEndTranslation.width
                )
                if projectedWidth <= -min(80, drawerWidth * 0.25) {
                    dismissSidebar()
                } else {
                    settleDrawerDrag()
                }
            }
    }

    private func sidebarOffset(drawerWidth: CGFloat) -> CGFloat {
        let restingOffset = isSidebarPresented ? 0 : -drawerWidth
        return min(0, max(-drawerWidth, restingOffset + drawerDragOffset))
    }

    private func sidebarRevealProgress(drawerWidth: CGFloat, drawerOffset: CGFloat) -> CGFloat {
        guard drawerWidth > 0 else { return 0 }
        return min(1, max(0, 1 + drawerOffset / drawerWidth))
    }

    private func presentSidebar() {
        withAnimation(drawerAnimation) {
            isSidebarPresented = true
            drawerDragOffset = 0
        }
    }

    private func dismissSidebar() {
        withAnimation(drawerAnimation) {
            isSidebarPresented = false
            drawerDragOffset = 0
        }
    }

    private func settleDrawerDrag() {
        withAnimation(drawerAnimation) {
            drawerDragOffset = 0
        }
    }

    private var drawerAnimation: Animation? {
        reduceMotion ? nil : .snappy(duration: 0.28, extraBounce: 0)
    }

    @ViewBuilder
    private var detail: some View {
        if model.isRestoring {
            ProgressView("Opening your chats…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(CagenticTheme.stage)
        } else if let conversationID = model.selectedConversationID {
            ChatView(model: model, conversationID: conversationID)
                .id(conversationID)
        } else {
            UnselectedChatView(model: model)
        }
    }

    @ViewBuilder
    private func sheet(for destination: AppSheet) -> some View {
        switch destination {
        case .connection:
            ConnectionSetupView(model: model)
        case .serverManager:
            ServerManagerView(model: model)
        case .settings:
            SettingsView(model: model)
        case .modelLibrary:
            ModelPickerView(model: model)
                .presentationDetents([.fraction(0.74), .large])
                .presentationDragIndicator(.visible)
        case .conversationManager:
            ConversationManagerView(model: model)
                .presentationDetents([.large])
                .presentationDragIndicator(.visible)
        case .renameConversation(let id):
            RenameConversationView(model: model, conversationID: id)
        }
    }

    private var onboardingPresentation: Binding<Bool> {
        Binding(
            get: { isOnboardingVisible },
            set: { _ in }
        )
    }

    private var isOnboardingVisible: Bool {
        !model.isRestoring && !model.settings.hasCompletedOnboarding
    }
}

private struct UnselectedChatView: View {
    @Bindable var model: AppModel

    var body: some View {
        ContentUnavailableView {
            Label {
                Text("Your local workspace")
            } icon: {
                BrandMark(size: 30)
            }
        } description: {
            Text(description)
        } actions: {
            Button(action: primaryAction) {
                Text(primaryTitle)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(isConnecting)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(CagenticTheme.stage)
    }

    private var description: String {
        if isConnecting {
            return "Refreshing the models available from your Ollama server."
        }
        if !model.connectionState.isConnected {
            return "Connect this device to Ollama on your computer to begin."
        }
        guard !model.activeModelIdentifier.isEmpty else {
            return "No models are available yet. Pull a model on the computer, then refresh here."
        }
        return "Create a chat to start working with \(model.activeModelName)."
    }

    private var primaryTitle: String {
        if isConnecting {
            return "Refreshing…"
        }
        if !model.connectionState.isConnected {
            return "Connect to Ollama"
        }
        return model.activeModelIdentifier.isEmpty ? "Refresh models" : "New chat"
    }

    private func primaryAction() {
        guard !isConnecting else { return }
        if !model.connectionState.isConnected {
            model.presentedSheet = .serverManager
        } else if model.activeModelIdentifier.isEmpty {
            Task {
                await model.refreshConnection()
            }
        } else {
            model.createConversation()
        }
    }

    private var isConnecting: Bool {
        if case .connecting = model.connectionState {
            return true
        }
        return false
    }
}

#Preview("Root · empty") {
    RootView(model: .preview())
}
