from pathlib import Path

import AVFoundation
import CoreMedia
import Foundation
import objc
import Quartz
import Vision

FRAME_INTERVAL_SECONDS = 2.0
MAX_FRAMES_PER_VIDEO = 45
# On-screen captions stay readable at this size, and larger frames only cost time.
FRAME_MAX_PIXELS = 1280
MIN_CONFIDENCE = 0.5
# Automatic language detection makes Vision compile a model per script it guesses at, which takes about 15 s per video.
LANGUAGES = ["en-US", "de-DE", "fr-FR", "es-ES", "it-IT", "pt-BR"]
# Shorter lines are almost always noise from textures and logos.
MIN_LINE_CHARACTERS = 2


class OcrError(Exception):
    pass


def _recognize(image) -> list[str]:
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    request.setRecognitionLanguages_(LANGUAGES)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise OcrError(str(error))
    lines = []
    for observation in request.results() or []:
        candidate = observation.topCandidates_(1)
        if candidate and candidate[0].confidence() >= MIN_CONFIDENCE:
            lines.append(str(candidate[0].string()).strip())
    return [line for line in lines if sum(character.isalnum() for character in line) >= MIN_LINE_CHARACTERS]


def _image_lines(path: Path) -> list[str]:
    source = Quartz.CGImageSourceCreateWithURL(Foundation.NSURL.fileURLWithPath_(str(path)), None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None) if source else None
    if image is None:
        raise OcrError(f"Could not read {path.name} as an image.")
    return _recognize(image)


def _video_lines(path: Path) -> list[str]:
    asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(Foundation.NSURL.fileURLWithPath_(str(path)), None)
    duration = CoreMedia.CMTimeGetSeconds(asset.duration())
    if not duration or duration != duration:
        raise OcrError(f"Could not read {path.name} as a video.")
    generator = AVFoundation.AVAssetImageGenerator.assetImageGeneratorWithAsset_(asset)
    generator.setAppliesPreferredTrackTransform_(True)
    generator.setMaximumSize_(Quartz.CGSizeMake(FRAME_MAX_PIXELS, FRAME_MAX_PIXELS))
    tolerance = CoreMedia.CMTimeMakeWithSeconds(FRAME_INTERVAL_SECONDS / 2, 600)
    generator.setRequestedTimeToleranceBefore_(tolerance)
    generator.setRequestedTimeToleranceAfter_(tolerance)

    interval = max(FRAME_INTERVAL_SECONDS, duration / MAX_FRAMES_PER_VIDEO)
    lines = []
    moment = min(0.5, duration / 2)
    while moment < duration:
        with objc.autorelease_pool():
            image = generator.copyCGImageAtTime_actualTime_error_(CoreMedia.CMTimeMakeWithSeconds(moment, 600), None, None)[0]
            if image is not None:
                lines.extend(_recognize(image))
            del image
        moment += interval
    return lines


def read_text(path: Path) -> str:
    lines = _video_lines(path) if path.suffix == ".mp4" else _image_lines(path)
    seen = {}
    for line in lines:
        seen.setdefault(line.casefold(), line)
    return "\n".join(seen.values())
