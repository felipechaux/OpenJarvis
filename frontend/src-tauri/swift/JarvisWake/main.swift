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

private let kWakePhrases: [String] = [
    "jarvis",
    "hi jarvis",
    "hey jarvis",
    "hello jarvis",
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
private func matchedWakePhrase(in text: String) -> String? {
    let lowered = text.lowercased()
    for phrase in kWakePhrases {
        let padded = " \(lowered) "
        let needle = " \(phrase) "
        if padded.contains(needle) {
            return phrase
        }
        if lowered.hasPrefix("\(phrase) ") || lowered.hasPrefix("\(phrase),")
            || lowered == phrase {
            return phrase
        }
    }
    return nil
}

// MARK: - WakeListener

final class WakeListener: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    private let captureSession = AVCaptureSession()
    private let audioOutput = AVCaptureAudioDataOutput()
    private let audioQueue = DispatchQueue(label: "com.openjarvis.wake.audio")
    private let recognizer: SFSpeechRecognizer

    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var segmentTimer: DispatchSourceTimer?

    private var lastWake: Date = .distantPast

    init(recognizer: SFSpeechRecognizer) {
        self.recognizer = recognizer
        super.init()
    }

    static func makeOrEmit() -> WakeListener? {
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")) else {
            emitError("SFSpeechRecognizer unavailable for en-US")
            return nil
        }
        if !recognizer.supportsOnDeviceRecognition {
            emitError("On-device recognition not supported on this Mac")
            return nil
        }
        return WakeListener(recognizer: recognizer)
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

        emit(["event": "ready", "device": device.localizedName])
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
        request?.appendAudioSampleBuffer(sampleBuffer)
    }

    // MARK: - Segment lifecycle

    /// Arm a fresh recognition request.  Called once at startup and again
    /// every ``kSegmentSeconds`` so we don't trip SFSpeechRecognitionTask's
    /// internal duration cap.
    private func startSegment() {
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.requiresOnDeviceRecognition = true
        // contextualStrings biases the language model toward our wake words.
        req.contextualStrings = kWakePhrases
        self.request = req

        task = recognizer.recognitionTask(with: req) { [weak self] result, error in
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
                        ])
                        self.rotateSegment()
                    }
                }
            }
            if error != nil {
                self.rotateSegmentIfActive()
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
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            self?.startSegment()
        }
    }

    private func rotateSegmentIfActive() {
        if request != nil {
            rotateSegment()
        }
    }
}

// MARK: - Entrypoint

guard let listener = WakeListener.makeOrEmit() else { exit(2) }
listener.start()
RunLoop.main.run()
