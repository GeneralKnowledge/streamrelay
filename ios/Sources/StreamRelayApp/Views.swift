#if os(iOS)
import AVFoundation
import StreamRelayCapture
import StreamRelayCore
import SwiftUI
import UIKit

@main
struct StreamRelayApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            Group {
                if model.paired { RecordView() } else { PairView() }
            }
            .environment(model)
            .task { model.launch() }
            .onChange(of: phase) { _, p in
                if p == .background { model.enteredBackground() }
                if p == .active { model.enteredForeground() }
            }
        }
    }
}

struct PairView: View {
    @Environment(AppModel.self) private var model
    @State private var code = ""
    @State private var busy = false

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                Section {
                    TextField("https://relay.example.com", text: $model.serverURL)
                        .textContentType(.URL).keyboardType(.URL).autocorrectionDisabled().textInputAutocapitalization(.never)
                        .accessibilityIdentifier("pair.server")
                        .accessibilityLabel("Server address")
                } header: { Text("Server") }
                Section {
                    TextField("ABCD-EFGH", text: $code)
                        .autocorrectionDisabled().textInputAutocapitalization(.characters)
                        .accessibilityIdentifier("pair.code")
                        .accessibilityLabel("Pairing code")
                } header: { Text("Pairing code") } footer: {
                    Text("Make a code on the server with streamrelay-admin pair-code. It works once, for ten minutes.")
                }
                if let p = model.problem {
                    Text(p).foregroundStyle(.red).accessibilityIdentifier("pair.problem")
                }
                Button(busy ? "Pairing…" : "Pair this iPhone") {
                    busy = true
                    Task { await model.pair(code: code); busy = false }
                }
                .disabled(busy || code.count < 8 || model.serverURL.isEmpty)
                .accessibilityIdentifier("pair.submit")
            }
            .navigationTitle("StreamRelay")
        }
    }
}

struct RecordView: View {
    @Environment(AppModel.self) private var model
    @State private var showSettings = false

    var body: some View {
        ZStack(alignment: .bottom) {
            if let r = model.recorder {
                CameraPreview(session: r.session).ignoresSafeArea()
                    .accessibilityHidden(true)
            } else {
                Color.black.ignoresSafeArea()
            }
            VStack(spacing: 12) {
                StatusPanel()
                HStack {
                    Button { showSettings = true } label: {
                        Image(systemName: "gearshape").font(.title2).frame(width: 52, height: 52)
                    }
                    .disabled(model.recording)
                    .accessibilityIdentifier("record.settings")
                    .accessibilityLabel("Settings")
                    Spacer()
                    Button { model.recording ? model.stop() : model.start() } label: {
                        Circle().fill(model.recording ? .red : .white).frame(width: 72, height: 72)
                            .overlay(Circle().stroke(.white, lineWidth: 4).padding(-6))
                    }
                    .accessibilityIdentifier("record.toggle")
                    .accessibilityLabel(model.recording ? "Stop broadcast" : (model.unfinished != nil ? "Resume broadcast" : "Start broadcast"))
                    Spacer()
                    Color.clear.frame(width: 52, height: 52)
                }
                .padding(.horizontal, 24)
            }
            .padding(.bottom, 24)
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
    }
}

struct StatusPanel: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if model.recording { Label("Recording, epoch \(model.epoch ?? 0)", systemImage: "record.circle") }
            Text(model.uploadLine).accessibilityIdentifier("record.upload")
            Text(String(format: "Waiting to upload: %d segments (%.0f s)", model.backlog.segments, model.backlog.seconds))
                .accessibilityIdentifier("record.backlog")
            if model.backlog.damaged > 0 { Text("\(model.backlog.damaged) segments rejected by the server") }
            if let h = model.hoursLeft { Text(String(format: "Storage for about %.1f hours", h)) }
            if let p = model.problem { Text(p).foregroundStyle(.yellow).accessibilityIdentifier("record.problem") }
        }
        .font(.footnote.monospacedDigit())
        .foregroundStyle(.white)
        .padding(10)
        .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityElement(children: .combine)
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        @Bindable var model = model
        NavigationStack {
            Form {
                Section("Broadcast") {
                    TextField("Title", text: $model.title).accessibilityIdentifier("settings.title")
                    Picker("After a drop-out", selection: $model.mode) {
                        Text("Catch up").tag(CatchUpMode.catchUp)
                        Text("Hold").tag(CatchUpMode.hold)
                        Text("Skip to live").tag(CatchUpMode.skipToLive)
                    }
                    .accessibilityIdentifier("settings.mode")
                    if model.mode == .hold {
                        Stepper("Hold at most \(model.holdMaxDelayMinutes) min", value: $model.holdMaxDelayMinutes, in: 1...120)
                            .accessibilityIdentifier("settings.holdMinutes")
                    }
                }
                Section("Server") {
                    Text(model.serverURL).foregroundStyle(.secondary)
                    Button("Unpair this iPhone", role: .destructive) { model.unpair(); dismiss() }
                        .accessibilityIdentifier("settings.unpair")
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { dismiss() }.accessibilityIdentifier("settings.done") }
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }

    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        return v
    }

    func updateUIView(_ view: PreviewView, context: Context) {}
}
#endif
