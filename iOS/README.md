# Cagentic for iOS

Cagentic is a native SwiftUI client for talking to a computer on the same
network. It speaks two backends: **Ollama** directly, for plain model chat that
stays on the device, and the **Cagentic gateway**, for an agentic assistant that
can read files, run commands, and search — asking your approval each time.

## Highlights

- Direct Ollama connection over the local network
- Cagentic gateway connection with streaming tool activity and approval prompts
- Multiple named servers with per-server model choices and Keychain credentials
- Streaming chat with live Markdown tables, nested lists, math, thinking output, and Stop
- Photo thumbnails plus previews for bounded text, source-code, and searchable-PDF attachments
- Persistent per-chat drafts, attachment drafts, branching, and a short-lived rewrite Undo
- Pinned and archived chats with rename, bulk management, Markdown export, and sharing
- Compact model selection, capability/context metadata, model installation, and token details
- Required first-launch guidance with actionable connection diagnostics
- iPhone and iPad navigation, Dynamic Type, VoiceOver, Reduce Motion, and full
  light/dark appearance support
- No third-party runtime dependencies

## Choose a backend

Each saved server records which backend it speaks, so both can be configured at
once and switched between.

| | Ollama | Cagentic gateway |
| --- | --- | --- |
| Default port | 11434 | 8700 |
| Chats live | on this device | on the computer, mirrored here for display |
| Credential | optional proxy bearer token | required access token |
| Replies | text only | text plus plans, tool calls, and approvals |
| Attachments | photos, PDFs, text, source code | not supported |
| Edit, regenerate, branch | yes | no — the gateway owns its history |

## Connect the app to the Cagentic gateway

The gateway listens only on the computer itself and mints a fresh token on every
restart, so two settings in `~/.config/cagentic/config.json` are required before
a phone can reach it:

```json
{
  "gateway": {
    "lan": true,
    "token": "a-long-random-string"
  }
}
```

`lan` binds the gateway to all interfaces; without a pinned `token` a paired
device breaks every time the gateway restarts. Restart the gateway, allow its
port through the computer's private-network firewall, then enter the computer's
LAN address and that token in the app.

> The gateway token grants full control of the computer — anything Cagentic can
> run, read, or change. Cagentic sends it over plain HTTP only to private LAN
> addresses, and shows a warning when it does; any routable host must be entered
> as an explicit `https://` URL. Never expose the gateway to the internet without
> an authenticated HTTPS reverse proxy.

In gateway mode the app is a pure `/api/*` client: with `gateway.lan` enabled the
gateway still refuses to serve its HTML page to anything but loopback, so the
phone never loads the web UI. Conversations belong to the gateway — the sidebar
mirrors its chat list, and creating, opening, renaming, or deleting a chat acts
on the computer. The gateway runs one turn at a time and shares that turn with
the terminal REPL and the web UI, so a message can be refused while it is busy;
the app restores the draft when that happens.

## Connect the app to a PC

Ollama listens only on the computer itself by default. To make it reachable by
an iPhone or iPad:

1. Configure Ollama on the computer with `OLLAMA_HOST=0.0.0.0:11434`, then fully
   restart Ollama. Follow the environment-variable steps for your operating
   system in the [official Ollama FAQ](https://docs.ollama.com/faq).
2. Allow inbound TCP port `11434` on the computer's **private** network in its
   firewall.
3. Put the iPhone/iPad and computer on the same trusted Wi-Fi. Disable a VPN or
   guest-network client isolation if it prevents devices from seeing each other.
4. Find the computer's LAN address (`ipconfig` on Windows, or the Network pane
   on macOS). Enter a URL such as `http://192.168.1.42:11434` in Cagentic.
5. Accept the iOS Local Network permission prompt, test the connection, and pick
   one of the models returned by Ollama.

`localhost`, `127.0.0.1`, `0.0.0.0`, and `::` are intentionally rejected in the
app. On iOS they identify the phone or a bind address, not the computer.

> Important: a default local Ollama server has no authentication. Binding it to
> all interfaces exposes its API to other devices on that network. Use trusted
> private networks only. For remote or untrusted access, put Ollama behind an
> authenticated HTTPS reverse proxy or a private VPN. Never expose port 11434
> directly to the public internet.

Bearer tokens are accepted only for `https://` endpoints. Cagentic refuses to
attach credentials to cleartext HTTP requests, including requests on a local
network.

## Attach files and photos

Use the composer’s plus menu to select images from Photos or choose supported
images, PDFs, text, and source-code files from Files. Photos are downsampled and
stripped of metadata before storage, then base64 encoded only while an Ollama
request is being assembled. Image messages require a model whose `/api/show`
capabilities include `vision`.

Searchable PDFs and text files are converted to bounded local reference text.
Scanned or encrypted PDFs need OCR or an unlocked copy before import. Persistent
chat snapshots contain attachment metadata only; payloads remain in protected
Application Support storage. Draft attachments survive relaunches and payloads are
removed once no persisted draft, message, branch, or short-lived Undo state refers
to them.

## Build

Requirements:

- Xcode 26 or newer (Swift 6.2 or newer)
- iOS 26 SDK or newer; the app deploys back to iOS 18
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) to regenerate the project

Generate the checked-in Xcode project after adding or moving Swift files:

```bash
xcodegen generate --spec iOS/project.yml
```

Then open `iOS/Cagentic.xcodeproj`, or build from the repository root:

```bash
xcodebuild \
  -project iOS/Cagentic.xcodeproj \
  -scheme Cagentic \
  -destination 'platform=iOS Simulator,name=iPhone 17' \
  -derivedDataPath /private/tmp/Cagentic-iOS-DerivedData \
  CODE_SIGNING_ALLOWED=NO \
  build
```

Run tests with the same destination and `test` in place of `build`.

## Project structure

```text
iOS/
├── project.yml                  XcodeGen source of truth
├── design-system/brand-spec.md  Native visual and interaction contract
├── Cagentic/
│   ├── App/                     App shell and dependency wiring
│   ├── Attachments/             Bounded import, protected storage, request payloads
│   ├── DesignSystem/            Tokens, styles, and Cagentic mark
│   ├── Features/                Chat, history, connection, models, settings
│   ├── Models/                  Durable app-domain values
│   ├── Networking/              Direct Ollama API and NDJSON streaming
│   ├── Persistence/             Atomic JSON snapshots and Keychain token
│   └── Resources/               Info.plist and asset catalog
└── CagenticTests/               Network, store, and persistence tests
```

`Networking/` holds both transports: `OllamaClient` speaks Ollama's native
NDJSON API on port `11434`, and `GatewayClient` speaks the gateway's
token-authenticated SSE protocol on port `8700`. They are deliberately separate
types — the two disagree about default ports, legal paths, who owns the
conversation, and whether a credential may cross the network unencrypted.

## Troubleshooting

- **Cannot reach server:** verify the PC address did not change, Ollama was
  restarted after setting `OLLAMA_HOST`, both devices are on the same private
  network, and the firewall allows TCP 11434.
- **Local Network denied:** open Settings → Privacy & Security → Local Network
  on the iPhone or iPad and enable Cagentic.
- **No models:** run `ollama pull <model>` on the computer, then refresh the
  model list in the app.
- **First response is slow:** Ollama may be loading the model into memory. The
  app keeps a generous streaming timeout and offers Stop at all times.
- **Connection drops mid-answer:** Cagentic preserves the partial response and
  exposes Retry once the network is available again.
- **Photos are unavailable:** choose a model that reports the `vision`
  capability, then send the preserved photo draft again.
- **A PDF cannot be attached:** confirm it is unlocked and contains selectable
  text; scanned PDFs require OCR first.
- **Gateway rejects the token:** confirm `gateway.token` in the computer's
  `config.json` matches what was entered, and that the gateway was restarted
  after the change.
- **Gateway unreachable:** confirm `gateway.lan` is `true`, the gateway was
  restarted, and the firewall allows its port. The gateway probes for a free
  port when 8700 is taken, so check the port it actually reported.
- **"The gateway is busy":** it runs one turn at a time, shared with the terminal
  and the web UI, and its own background routines take the same lock. The message
  was not sent; the draft is restored so it can be sent again.
