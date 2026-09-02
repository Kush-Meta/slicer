// vision_ocr — Apple Vision text recognition with line boxes and confidence.
//
// Reads an image, emits JSON on stdout:
//   { "width": Int, "height": Int, "lines": [ {text, confidence, x, y, w, h} ] }
//
// Coordinates are pixels in the image's own space, origin top-left, so they
// compose directly with capture rectangles. Vision reports normalized boxes
// with a bottom-left origin; the flip happens here so no caller has to know.

import Foundation
import Vision
import AppKit

struct Line: Codable {
    let text: String
    let confidence: Float
    let x: Int, y: Int, w: Int, h: Int
}

struct Output: Codable {
    let width: Int
    let height: Int
    let lines: [Line]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(("vision_ocr: " + message + "\n").data(using: .utf8)!)
    exit(2)
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: vision_ocr <image> [--fast] [--langs en-US,fr-FR]") }

let path = args[1]
var fastMode = false
var languages: [String] = ["en-US"]

var i = 2
while i < args.count {
    switch args[i] {
    case "--fast": fastMode = true
    case "--langs":
        guard i + 1 < args.count else { fail("--langs needs a value") }
        languages = args[i + 1].split(separator: ",").map(String.init)
        i += 1
    default: fail("unknown flag \(args[i])")
    }
    i += 1
}

guard let image = NSImage(contentsOfFile: path),
      let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cgImage = bitmap.cgImage
else { fail("could not read image at \(path)") }

let pixelWidth = cgImage.width
let pixelHeight = cgImage.height

let request = VNRecognizeTextRequest()
request.recognitionLevel = fastMode ? .fast : .accurate
request.usesLanguageCorrection = !fastMode
request.recognitionLanguages = languages

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("recognition failed: \(error.localizedDescription)")
}

var lines: [Line] = []
for observation in (request.results ?? []) {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox   // normalized, origin bottom-left

    let px = box.origin.x * CGFloat(pixelWidth)
    let pw = box.size.width * CGFloat(pixelWidth)
    let ph = box.size.height * CGFloat(pixelHeight)
    // flip the origin: Vision measures up from the bottom, screens measure down
    let py = (1.0 - box.origin.y - box.size.height) * CGFloat(pixelHeight)

    lines.append(Line(
        text: candidate.string,
        confidence: candidate.confidence,
        x: Int(px.rounded()), y: Int(py.rounded()),
        w: Int(pw.rounded()), h: Int(ph.rounded())
    ))
}

let output = Output(width: pixelWidth, height: pixelHeight, lines: lines)
let encoder = JSONEncoder()
encoder.outputFormatting = [.withoutEscapingSlashes]
guard let data = try? encoder.encode(output) else { fail("could not encode result") }
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
