import CoreMedia
import Foundation
import ScreenCaptureKit
import VideoToolbox

private let startCode: [UInt8] = [0, 0, 0, 1]

private func writeEncodedFrame(
    _ context: UnsafeMutableRawPointer?, _ source: UnsafeMutableRawPointer?,
    _ status: OSStatus, _ flags: VTEncodeInfoFlags, _ sample: CMSampleBuffer?
) {
    guard status == noErr, let sample, let format = sample.formatDescription,
          let block = sample.dataBuffer else { return }
    var frame = Data()
    let attachments = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false)
        as? [[String: Any]]
    let isKeyframe = attachments?.first?[kCMSampleAttachmentKey_NotSync as String] as? Bool != true
    if isKeyframe {
        var count = 0
        guard CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
            format, parameterSetIndex: 0, parameterSetPointerOut: nil,
            parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil
        ) == noErr else { return }
        for index in 0..<count {
            var pointer: UnsafePointer<UInt8>?
            var size = 0
            if CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                format, parameterSetIndex: index, parameterSetPointerOut: &pointer,
                parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil
            ) == noErr, let pointer {
                frame.append(contentsOf: startCode)
                frame.append(pointer, count: size)
            }
        }
    }
    var bytes = Data(count: CMBlockBufferGetDataLength(block))
    let copyStatus = bytes.withUnsafeMutableBytes { buffer -> OSStatus in
        guard let address = buffer.baseAddress else { return -1 }
        return CMBlockBufferCopyDataBytes(
            block, atOffset: 0, dataLength: buffer.count, destination: address
        )
    }
    guard copyStatus == noErr else { return }

    var offset = 0
    while offset + 4 <= bytes.count {
        let length = bytes[offset..<offset + 4].reduce(0) { ($0 << 8) | Int($1) }
        offset += 4
        guard length > 0, length <= bytes.count - offset else { return }
        frame.append(contentsOf: startCode)
        frame.append(contentsOf: bytes[offset..<offset + length])
        offset += length
    }
    guard offset == bytes.count else { return }
    do {
        try FileHandle.standardOutput.write(contentsOf: frame)
    } catch {
        exit(1)
    }
}

private enum CaptureError: Error {
    case noDisplay
    case encoder(OSStatus)
}

private final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let session: VTCompressionSession
    private let captureQueue = DispatchQueue(label: "remote-mac.capture")

    init(width: Int, height: Int) throws {
        var encoder: VTCompressionSession?
        let specification = [
            kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder: true,
            kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true
        ] as CFDictionary
        let status = VTCompressionSessionCreate(
            allocator: nil, width: Int32(width), height: Int32(height),
            codecType: kCMVideoCodecType_H264, encoderSpecification: specification,
            imageBufferAttributes: nil, compressedDataAllocator: nil,
            outputCallback: writeEncodedFrame, refcon: nil, compressionSessionOut: &encoder
        )
        guard status == noErr, let encoder else { throw CaptureError.encoder(status) }
        session = encoder
        super.init()

        let properties: [(CFString, CFTypeRef)] = [
            (kVTCompressionPropertyKey_RealTime, kCFBooleanTrue),
            (kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse),
            (kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: 60)),
            (kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: 24_000_000)),
            (kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, NSNumber(value: 1))
        ]
        for (key, value) in properties {
            let status = VTSessionSetProperty(encoder, key: key, value: value)
            guard status == noErr else { throw CaptureError.encoder(status) }
        }
        let prepareStatus = VTCompressionSessionPrepareToEncodeFrames(encoder)
        guard prepareStatus == noErr else { throw CaptureError.encoder(prepareStatus) }
    }

    func makeStream(display: SCDisplay, width: Int, height: Int) throws -> SCStream {
        let config = SCStreamConfiguration()
        config.width = width
        config.height = height
        config.minimumFrameInterval = CMTime(value: 1, timescale: 60)
        config.queueDepth = 3
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.showsCursor = true
        let stream = SCStream(
            filter: SCContentFilter(display: display, excludingWindows: []),
            configuration: config, delegate: self
        )
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: captureQueue)
        return stream
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, let image = sample.imageBuffer,
              let info = (CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false)
                          as? [[SCStreamFrameInfo: Any]])?.first,
              let status = info[.status] as? Int,
              status == SCFrameStatus.complete.rawValue else { return }
        let result = VTCompressionSessionEncodeFrame(
            session, imageBuffer: image, presentationTimeStamp: sample.presentationTimeStamp,
            duration: .invalid, frameProperties: nil, sourceFrameRefcon: nil, infoFlagsOut: nil
        )
        if result != noErr {
            fputs("VideoToolbox encode failed: \(result)\n", stderr)
            exit(1)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        fputs("Screen capture stopped: \(error)\n", stderr)
        exit(1)
    }
}

@main
struct Main {
    static func main() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let target = CommandLine.arguments.dropFirst().first.flatMap(UInt32.init) ?? CGMainDisplayID()
        guard let display = content.displays.first(where: { $0.displayID == target })
            else { throw CaptureError.noDisplay }
        let mode = CGDisplayCopyDisplayMode(display.displayID)
        let sourceWidth = mode?.pixelWidth ?? display.width
        let sourceHeight = mode?.pixelHeight ?? display.height
        let scale = min(2560.0 / Double(sourceWidth), 1800.0 / Double(sourceHeight), 1)
        let width = Int(Double(sourceWidth) * scale) / 2 * 2
        let height = Int(Double(sourceHeight) * scale) / 2 * 2
        let capture = try Capture(width: width, height: height)
        let stream = try capture.makeStream(display: display, width: width, height: height)
        try await stream.startCapture()
        fputs("Capturing \(width)×\(height) at up to 60 fps\n", stderr)
        while true {
            try await Task.sleep(nanoseconds: 60_000_000_000)
            withExtendedLifetime((capture, stream)) {}
        }
    }
}
