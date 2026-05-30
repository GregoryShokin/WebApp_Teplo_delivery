#!/usr/bin/env swift
import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("Usage: ocr_image_macos.swift <image-path>")
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])

var recognizedLines: [String] = []
var recognitionError: Error?

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        recognitionError = error
        return
    }
    let observations = request.results as? [VNRecognizedTextObservation] ?? []
    recognizedLines = observations.compactMap { observation in
        observation.topCandidates(1).first?.string
    }
}

request.recognitionLevel = .accurate
let preferredLanguages = ["ru-RU", "en-US", "ru", "en"]
let supportedLanguages = (try? request.supportedRecognitionLanguages()) ?? []
let selectedLanguages = preferredLanguages.filter { supportedLanguages.contains($0) }
if !selectedLanguages.isEmpty {
    request.recognitionLanguages = selectedLanguages
}
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(url: imageURL, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("OCR failed: \(error.localizedDescription)")
}

if let error = recognitionError {
    fail("OCR failed: \(error.localizedDescription)")
}

print(recognizedLines.joined(separator: "\n"))
