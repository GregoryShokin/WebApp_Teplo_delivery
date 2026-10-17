#!/usr/bin/env python3
"""Extract source frames by index, preserving actual PTS and source identity.

No action recognition, clock calibration, timer masking or external upload.
Python standard library plus ffprobe/ffmpeg; Pillow only for --contact-sheet.
"""

import argparse
import bisect
import hashlib
import json
import math
import shutil
import subprocess
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path, help="New directory; never overwritten")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--at", type=float, nargs="+", help="Seconds from first video frame")
    group.add_argument("--frames", type=int, nargs="+", help="Zero-based source frame indices")
    parser.add_argument("--contact-sheet", action="store_true")
    args = parser.parse_args()
    source = args.source.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error("Output already exists; choose a new directory")
    for binary in ("ffprobe", "ffmpeg"):
        if not shutil.which(binary):
            parser.error(f"Required binary not found: {binary}")
    requested = args.at if args.at is not None else args.frames
    if len(requested) > 240:
        parser.error("Maximum 240 requested frames per extraction; split into batches")
    if any(not math.isfinite(value) or value < 0 for value in requested):
        parser.error("Requested times/indices must be finite and non-negative")
    if args.contact_sheet:
        from PIL import Image, ImageDraw

    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_streams", "-show_format", "-show_entries",
        "frame=best_effort_timestamp_time:stream=index,codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames:format=duration,size",
        "-of", "json", str(source),
    ], text=True))
    frames = probe.get("frames", [])
    if not frames:
        parser.error("No decoded video frames")
    try:
        pts = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    except (KeyError, ValueError):
        parser.error("Missing/invalid frame PTS; inspect source manually")
    if any(not math.isfinite(value) for value in pts):
        parser.error("Non-finite PTS; inspect source manually")
    if any(right < left for left, right in zip(pts, pts[1:])):
        parser.error("Non-monotonic PTS; inspect discontinuity before extraction")
    requests = []
    if args.at is not None:
        for elapsed in args.at:
            if elapsed > pts[-1] - pts[0] + 1e-6:
                parser.error(f"Requested elapsed time beyond last frame: {elapsed}")
            target = pts[0] + elapsed
            position = bisect.bisect_left(pts, target)
            candidates = [i for i in (position - 1, position) if 0 <= i < len(pts)]
            index = min(candidates, key=lambda i: (abs(pts[i] - target), i))
            requests.append({"requested_elapsed_seconds": elapsed, "source_frame_index": index})
    else:
        for index in args.frames:
            if index >= len(frames):
                parser.error(f"Frame index beyond source: {index}")
            requests.append({"requested_frame_index": index, "source_frame_index": index})
    selected = sorted({request["source_frame_index"] for request in requests})
    source_hash = sha256(source)
    output.mkdir(parents=True)
    expression = "+".join(f"eq(n\\,{index})" for index in selected)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        "-i", str(source), "-map", "0:v:0", "-vf", f"select={expression}",
        "-fps_mode", "vfr", "-q:v", "2", str(output / "frame-%04d.jpg"),
    ], check=True)
    images = sorted(output.glob("frame-*.jpg"))
    if len(images) != len(selected):
        raise RuntimeError(f"Expected {len(selected)} frames, received {len(images)}; do not use mapping")
    records = []
    for index, path in zip(selected, images):
        records.append({
            "source_frame_index": index,
            "pts_seconds": pts[index],
            "elapsed_from_first_frame_seconds": pts[index] - pts[0],
            "file": path.name,
            "sha256": sha256(path),
        })
    manifest = {
        "schema_version": 1,
        "source": str(source), "source_sha256": source_hash,
        "stream": probe.get("streams", []), "format": probe.get("format", {}),
        "decoded_frame_count": len(frames), "first_pts_seconds": pts[0],
        "last_pts_seconds": pts[-1], "requests": requests, "frames": records,
        "warning": "Media timestamps only. Wall-clock continuity, action boundaries and timer masking are NOT validated.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if args.contact_sheet:
        columns, cell_width, cell_height = min(3, len(images)), 480, 330
        sheet = Image.new("RGB", (columns * cell_width, math.ceil(len(images) / columns) * cell_height), "white")
        draw = ImageDraw.Draw(sheet)
        for position, record in enumerate(records):
            x, y = (position % columns) * cell_width, (position // columns) * cell_height
            with Image.open(output / record["file"]) as original:
                thumb = original.convert("RGB")
                thumb.thumbnail((cell_width, cell_height - 42))
                sheet.paste(thumb, (x + (cell_width - thumb.width) // 2, y))
            label = f"frame {record['source_frame_index']} | PTS {record['pts_seconds']:.6f}"
            draw.text((x + 5, y + cell_height - 37), label, fill="black")
            draw.text((x + 5, y + cell_height - 20), f"elapsed {record['elapsed_from_first_frame_seconds']:.6f} s (media)", fill="black")
        sheet.save(output / "contact-sheet.jpg", quality=90)
    print(json.dumps({"output": str(output), "selected_frames": len(records), "manifest": str(output / "manifest.json")}))


if __name__ == "__main__":
    main()
