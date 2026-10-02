#!/usr/bin/env python3
"""Prepare full-frame images, contact sheets and optional audio for an AI agent."""

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1800:] or "媒体工具执行失败")
    return result.stdout


def prepare(video, output, interval=None, at=(), audio=False, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    tools = {name: shutil.which(value) for name, value in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        raise RuntimeError("缺少可用工具：" + ", ".join(missing) + "；请安装或提供其可执行文件路径。")
    video = Path(video).resolve()
    if not video.is_file():
        raise RuntimeError("找不到本地视频文件。")
    probe = json.loads(run([
        tools["ffprobe"], "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height", "-of", "json", str(video),
    ]))
    if not any(stream.get("codec_type") == "video" for stream in probe.get("streams", [])):
        raise RuntimeError("文件不包含视频轨，不能生成画面。")
    duration = float(probe["format"]["duration"])
    interval = interval if interval is not None else (1.0 if duration <= 120 else 3.0)
    if not math.isfinite(interval) or interval <= 0:
        raise RuntimeError("抽帧间隔必须为有限正数。")
    if any(not math.isfinite(t) or t < 0 or t >= duration for t in at):
        raise RuntimeError("指定画面的时间必须位于视频时长范围内。")
    output = Path(output).resolve()
    frames = output / "frames"
    sheets = output / "sheets"
    frames.mkdir(parents=True, exist_ok=True)
    sheets.mkdir(parents=True, exist_ok=True)
    for folder, pattern in ((frames, "frame-*.jpg"), (sheets, "sheet-*.jpg")):
        for old in folder.glob(pattern):
            old.unlink()
    run([
        tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
        "-vf", f"fps=1/{interval}", "-q:v", "2", str(frames / "frame-%05d.jpg"),
    ])
    images = sorted(frames.glob("frame-*.jpg"))
    if not images:
        raise RuntimeError("抽帧结果为空，请使用更短间隔或检查视频。")
    run([
        tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
        "-i", str(frames / "frame-%05d.jpg"), "-vf", "scale=480:-2,tile=3x3",
        "-frames:v", str(math.ceil(len(images) / 9)), "-q:v", "2", str(sheets / "sheet-%03d.jpg"),
    ])
    details = []
    for t in at:
        path = output / f"detail-{t:g}s.jpg"
        run([
            tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t),
            "-i", str(video), "-frames:v", "1", "-q:v", "2", str(path),
        ])
        if not path.is_file():
            raise RuntimeError("未能提取指定时间的画面。")
        details.append(str(path))
    audio_path = None
    if audio:
        if not any(s.get("codec_type") == "audio" for s in probe.get("streams", [])):
            raise RuntimeError("视频不包含音频轨。")
        audio_path = output / "audio.wav"
        run([
            tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
        ])
    manifest = {
        "video_path": str(video), "duration_seconds": duration, "interval_seconds": interval,
        "frame_count": len(images), "frames_directory": str(frames),
        "sheets": [str(p) for p in sorted(sheets.glob("sheet-*.jpg"))], "details": details,
        "audio_path": str(audio_path) if audio_path else None,
        "probe": probe, "frame_times": "regular samples; use --at for a precise seek",
        "recognition": "not_performed; an agent must inspect images or transcribe audio",
    }
    (output / "media.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--at", nargs="*", type=float, default=[])
    parser.add_argument("--audio", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    try:
        result = prepare(args.video, args.output or args.video.parent / "analysis", args.interval,
                         args.at, args.audio, args.ffmpeg, args.ffprobe)
        print(json.dumps({k: result[k] for k in (
            "duration_seconds", "frame_count", "frames_directory", "sheets", "details", "audio_path",
        )}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        print(f"视频准备失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
