// JarvisWake — native macOS wake-word sidecar.
//
// Listens to the default microphone via AVCaptureSession, runs continuous
// on-device speech recognition through Apple's Speech framework, and emits
// a single JSON line to stdout whenever the user says one of the wake
// phrases ("jarvis", "hey jarvis", "hi jarvis", "hello jarvis").
//
// Output format (one line per event):
//   {"event":"ready"}                                  // recognizer armed
//   {"event":"wake","text":"hey jarvis what time..."}  // wake phrase heard
//   {"event":"error","message":"..."}                  // fatal
//
// Tauri's Rust shell spawns this binary, parses each stdout line, and
// re-broadcasts the wake event to the webview via Tauri's event system.
//
// AVCaptureSession is used (rather than AVAudioEngine) because the engine's
// input node can silently stop pumping audio after the first buffer on some
// macOS configurations — especially headless LSUIElement apps and Macs
// with multi-channel built-in mics.  AVCaptureSession is purpose-built for
// long-running capture and works reliably for both.

import AVFoundation
import Foundation
import Speech

// MARK: - Tunables

/// Locales we try to arm in parallel.  Each successful recognizer listens
/// to the same mic stream — either language can fire the wake.  This is
/// critical for bilingual users: Apple Speech in en-US misses "Jarvis"
/// pronounced with a Spanish accent (and vice versa for es-ES on English
/// commands), so we run both and trust whichever transcript matches first.
private let kLocales: [String] = ["es-ES", "en-US", "es-MX"]

/// Wake phrases in both languages.  Lowercased; whole-word matched.
private let kWakePhrases: [String] = [
    // English
    "jarvis",
    "hi jarvis",
    "hey jarvis",
    "hello jarvis",
    // Spanish
    "oye jarvis",
    "hola jarvis",
    "ey jarvis",
    "che jarvis",
]

/// Common mishearings of "jarvis" the recognizers produce when the speaker
/// has a non-native accent or background noise.  Treated as wake matches
/// even when no canonical phrase appears.  Excluded: real English words
/// ("travis", "drivers") that would cause constant false positives.
private let kFuzzyWakeTokens: [String] = [
    "jarvis", "jervis", "javis", "jarvey", "jarvi",
    "yarvis", "yarbis", "jarbis", "harvis", "charvis",
    "yarvi", "harvi", "jharvis", "jarvys", "jarvees",
]

/// Restart recognition before SFSpeechRecognitionTask hits its internal
/// duration limit (~60s).  50s leaves comfortable headroom.
private let kSegmentSeconds: TimeInterval = 50

/// After a wake event, ignore further detections for this long.  Prevents
/// the same utterance ("Jarvis, what time is it") from firing once for
/// "Jarvis" and again for the same partial transcript a moment later.
private let kCooldownSeconds: TimeInterval = 3

// MARK: - Stdout JSON helpers

private func emit(_ payload: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []),
          let line = String(data: data, encoding: .utf8) else {
        return
    }
    print(line)
    fflush(stdout)
}

private func emitError(_ message: String) {
    emit(["event": "error", "message": message])
}

// MARK: - Wake-phrase matcher

/// Returns the first wake phrase found in ``text``, or nil if none match.
/// Matches whole-word boundaries so "drivers" / "Travis" never trigger.
/// Also accepts common mishearings of "jarvis" (yarvis, jarbis, harvis…)
/// so a non-native accent doesn't have to fight the recognizer.
private func matchedWakePhrase(in text: String) -> String? {
    let lowered = text.lowercased()
    let padded = " \(lowered) "

    for phrase in kWakePhrases {
        let needle = " \(phrase) "
        if padded.contains(needle) {
            return phrase
        }
        if lowered.hasPrefix("\(phrase) ") || lowered.hasPrefix("\(phrase),")
            || lowered == phrase {
            return phrase
        }
    }

    // Fuzzy single-token match — catches "yarvis", "jarbis", "harvis", etc.
    for tok in kFuzzyWakeTokens {
        let needle = " \(tok) "
        if padded.contains(needle) {
            return tok
        }
        if lowered.hasPrefix("\(tok) ") || lowered.hasPrefix("\(tok),")
            || lowered == tok {
            return tok
        }
    }
    return nil
}

// MARK: - WakeListener

/// Tracks one SFSpeechRecognizer + its current in-flight request/task.
/// We hold one of these per locale so multiple languages can listen to the
/// same audio stream simultaneously.  ``fileprivate`` so ``WakeListener``'s
/// init can take ``[RecognizerSlot]`` without leaking it to other files.
fileprivate final class RecognizerSlot {
    let locale: String
    let recognizer: SFSpeechRecognizer
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?

    init(locale: String, recognizer: SFSpeechRecognizer) {
        self.locale = locale
        self.recognizer = recognizer
    }
}

final class WakeListener: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let captureSession = AVCaptureSession()
    private let audioOutput = AVCaptureAudioDataOutput()
    private let audioQueue = DispatchQueue(label: "com.openjarvis.wake.audio")
    private let slots: [RecognizerSlot]

    private var segmentTimer: DispatchSourceTimer?
    private var lastWake: Date = .distantPast

    fileprivate init(slots: [RecognizerSlot]) {
        self.slots = slots
        super.init()
    }

    /// Build a recognizer for every locale in ``kLocales`` that's actually
    /// available on this Mac and supports on-device recognition.  We need
    /// at least one to succeed — otherwise the sidecar emits an error and
    /// the JS fallback (Whisper polling) takes over.
    static func makeOrEmit() -> WakeListener? {
        var slots: [RecognizerSlot] = []
        for locale in kLocales {
            guard let rec = SFSpeechRecognizer(locale: Locale(identifier: locale)) else {
                FileHandle.standardError.write(
                    Data("[wake] SFSpeechRecognizer unavailable for \(locale)\n".utf8)
                )
                continue
            }
            // On-device is required: it's the only way we get a continuous,
            // privacy-preserving recognition session.  Cloud mode is rate-
            // limited and ships audio to Apple — both are unacceptable here.
            if !rec.supportsOnDeviceRecognition {
                FileHandle.standardError.write(
                    Data("[wake] on-device recognition unsupported for \(locale)\n".utf8)
                )
                continue
            }
            slots.append(RecognizerSlot(locale: locale, recognizer: rec))
        }
        if slots.isEmpty {
            emitError("No locales available with on-device speech recognition")
            return nil
        }
        return WakeListener(slots: slots)
    }

    /// Wait for both speech-recognition and microphone authorisation,
    /// then start the capture session and arm the first recognition segment.
    func start() {
        SFSpeechRecognizer.requestAuthorization { [weak self] status in
            guard let self = self else { return }
            switch status {
            case .authorized:
                self.requestMicAuthorization()
            case .denied:
                emitError("Speech recognition denied")
            case .restricted:
                emitError("Speech recognition restricted")
            case .notDetermined:
                emitError("Speech recognition undetermined")
            @unknown default:
                emitError("Speech recognition: unknown status")
            }
        }
    }

    private func requestMicAuthorization() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self = self else { return }
            if !granted {
                emitError("Microphone access denied")
                return
            }
            DispatchQueue.main.async {
                self.setupCapture()
            }
        }
    }

    private func setupCapture() {
        guard let device = AVCaptureDevice.default(for: .audio) else {
            emitError("No default audio device")
            return
        }

        let deviceInput: AVCaptureDeviceInput
        do {
            deviceInput = try AVCaptureDeviceInput(device: device)
        } catch {
            emitError("Audio input setup failed: \(error.localizedDescription)")
            return
        }

        captureSession.beginConfiguration()
        if captureSession.canAddInput(deviceInput) {
            captureSession.addInput(deviceInput)
        } else {
            emitError("Cannot add audio input to capture session")
            captureSession.commitConfiguration()
            return
        }

        audioOutput.setSampleBufferDelegate(self, queue: audioQueue)
        if captureSession.canAddOutput(audioOutput) {
            captureSession.addOutput(audioOutput)
        } else {
            emitError("Cannot add audio output to capture session")
            captureSession.commitConfiguration()
            return
        }
        captureSession.commitConfiguration()

        captureSession.startRunning()
        if !captureSession.isRunning {
            emitError("Capture session failed to start")
            return
        }

        let localeList = slots.map { $0.locale }.joined(separator: ",")
        emit([
            "event": "ready",
            "device": device.localizedName,
            "locales": localeList,
        ])
        startSegment()
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // appendAudioSampleBuffer accepts CMSampleBuffer directly and handles
        // mono/multi-channel + sample-rate conversion internally — much more
        // robust than feeding AVAudioPCMBuffer that the recogniser silently
        // ignores when the channel count doesn't match its expectations.
        // Fan out the same buffer to every active recognizer so each
        // language can transcribe it in parallel.
        for slot in slots {
            slot.request?.appendAudioSampleBuffer(sampleBuffer)
        }
    }

    // MARK: - Segment lifecycle

    /// Arm a fresh recognition request on every active slot.  Called once
    /// at startup and again every ``kSegmentSeconds`` so we don't trip
    /// SFSpeechRecognitionTask's internal duration cap.  A wake event in
    /// any slot fires the shared cooldown so we don't double-emit when both
    /// en-US and es-ES transcribe the same utterance.
    private func startSegment() {
        for slot in slots {
            let req = SFSpeechAudioBufferRecognitionRequest()
            req.shouldReportPartialResults = true
            req.requiresOnDeviceRecognition = true
            // contextualStrings biases the language model toward our wake
            // words.  Apple Speech in es-ES otherwise hears "Yarbis" /
            // "Charbi" instead of "Jarvis", and en-US hears "Travis".
            req.contextualStrings = kWakePhrases + kFuzzyWakeTokens
            slot.request = req

            let locale = slot.locale
            slot.task = slot.recognizer.recognitionTask(with: req) {
                [weak self] result, error in
                guard let self = self else { return }
                if let result = result {
                    let text = result.bestTranscription.formattedString
                    if let phrase = matchedWakePhrase(in: text) {
                        let now = Date()
                        if now.timeIntervalSince(self.lastWake) >= kCooldownSeconds {
                            self.lastWake = now
                            emit([
                                "event": "wake",
                                "phrase": phrase,
                                "text": text,
                                "locale": locale,
                            ])
                            self.rotateSegment()
                        }
                    }
                }
                if error != nil {
                    self.rotateSegmentIfActive()
                }
            }
        }

        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + kSegmentSeconds)
        timer.setEventHandler { [weak self] in self?.rotateSegment() }
        timer.resume()
        segmentTimer = timer
    }

    private func rotateSegment() {
        segmentTimer?.cancel()
        segmentTimer = nil
        for slot in slots {
            slot.request?.endAudio()
            slot.task?.cancel()
            slot.request = nil
            slot.task = nil
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            self?.startSegment()
        }
    }

    private func rotateSegmentIfActive() {
        if slots.contains(where: { $0.request != nil }) {
            rotateSegment()
        }
    }
}

// MARK: - Entrypoint

guard let listener = WakeListener.makeOrEmit() else { exit(2) }
listener.start()
RunLoop.main.run()
