import SwiftUI

@main
struct CagenticApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @State private var model: AppModel

    init() {
        CagenticTheme.configureUIKitTypography()
#if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("--preview-data") {
            let appearance: AppearancePreference = arguments.contains("--dark-mode") ? .dark : .light
            _model = State(
                initialValue: AppModel.preview(
                    appearance: appearance,
                    streaming: arguments.contains("--preview-streaming")
                )
            )
        } else {
            _model = State(initialValue: AppModel.live())
        }
#else
        _model = State(initialValue: AppModel.live())
#endif
    }

    var body: some Scene {
        WindowGroup {
            appRoot
        }
    }

    private var appRoot: some View {
        RootView(model: model)
            .font(CagenticTheme.FontStyle.body)
            .tint(CagenticTheme.accent)
            .background(CagenticTheme.stage.ignoresSafeArea())
            .preferredColorScheme(model.settings.appearance.colorScheme)
            .task {
                await model.start()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    model.cancelPersistenceFlush()
                    // A gateway's chats live on the computer, where the terminal and the web UI can
                    // change them while this device is in the background.
                    model.scheduleGatewayChatSync()
                } else {
                    model.requestPersistenceFlush()
                }
            }
    }
}
