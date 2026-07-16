import Foundation
import Vision
import CoreImage

// usage: swift cutout.swift <in> <out> [x y w h]  (crop rect, top-left origin, px)
let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: cutout <in> <out> [x y w h]\n".data(using: .utf8)!); exit(1) }
let inURL = URL(fileURLWithPath: args[1])
let outURL = URL(fileURLWithPath: args[2])

guard var img = CIImage(contentsOf: inURL) else { FileHandle.standardError.write("cannot read image\n".data(using: .utf8)!); exit(1) }
if args.count >= 7, let x = Double(args[3]), let y = Double(args[4]), let w = Double(args[5]), let h = Double(args[6]) {
    let ih = img.extent.height
    let rect = CGRect(x: x, y: ih - y - h, width: w, height: h)
    img = img.cropped(to: rect).transformed(by: CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
}

let req = VNGenerateForegroundInstanceMaskRequest()
let handler = VNImageRequestHandler(ciImage: img, options: [:])
do {
    try handler.perform([req])
    guard let obs = req.results?.first else { FileHandle.standardError.write("no foreground found\n".data(using: .utf8)!); exit(2) }
    print("instances: \(obs.allInstances.count)")
    let maskPB = try obs.generateScaledMaskForImage(forInstances: obs.allInstances, from: handler)
    let mask = CIImage(cvPixelBuffer: maskPB)

    let blend = CIFilter(name: "CIBlendWithMask")!
    blend.setValue(img, forKey: kCIInputImageKey)
    blend.setValue(CIImage(color: .clear).cropped(to: img.extent), forKey: kCIInputBackgroundImageKey)
    blend.setValue(mask, forKey: kCIInputMaskImageKey)
    guard let out = blend.outputImage else { FileHandle.standardError.write("blend failed\n".data(using: .utf8)!); exit(3) }

    let ctx = CIContext()
    let cs = CGColorSpace(name: CGColorSpace.sRGB)!
    try ctx.writePNGRepresentation(of: out, to: outURL, format: .RGBA8, colorSpace: cs)
    print("ok \(Int(out.extent.width))x\(Int(out.extent.height))")
} catch {
    FileHandle.standardError.write("error: \(error)\n".data(using: .utf8)!)
    exit(4)
}
