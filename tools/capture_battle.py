"""Read-only ADB observer for an already running MFA; no input commands."""

import os
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path

import cv2
import numpy as np

# Both CLI and embedded Agent use package imports rooted at agent/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from utils.image_io import decode_image
from produce.capture import DecisionCapture


def screencap(adb: str, serial: str):
    result = subprocess.run(
        [adb, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=15,
        check=True,
    )
    return decode_image(result.stdout)


def health_score(image, template):
    height, width = image.shape[:2]
    if abs(width / height - 720 / 1280) > 0.005:
        return None
    # Only the analysis copy is normalized; saved ADB frames remain untouched.
    frame = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_LANCZOS4) if (width, height) != (720, 1280) else image
    roi = frame[125:255, 512:680]
    blue, green, red = cv2.split(template)
    mask = np.where((green.astype(int) > red.astype(int) + 20) & (green.astype(int) > blue.astype(int) + 20), 0, 255).astype("uint8")
    if template.shape[0] > roi.shape[0] or template.shape[1] > roi.shape[1]:
        return None
    scores = cv2.matchTemplate(roi, template, cv2.TM_SQDIFF_NORMED, mask=mask)
    finite = scores[np.isfinite(scores)]
    return None if not finite.size else float(1 - finite.min())


def skip_score(image, templates):
    height, width = image.shape[:2]
    if abs(width / height - 720 / 1280) > 0.005:
        return None
    frame = cv2.resize(image, (720, 1280), interpolation=cv2.INTER_LANCZOS4) if (width, height) != (720, 1280) else image
    roi = frame[720:840, 590:720]
    scores = []
    for template in templates:
        if template.shape[0] <= roi.shape[0] and template.shape[1] <= roi.shape[1]:
            result = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
            finite = result[np.isfinite(result)]
            if finite.size:
                scores.append(float(finite.max()))
    return max(scores) if scores else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resource", type=Path, required=True)
    parser.add_argument("--database", type=Path, help="Pinned skill-card catalog; default: resource package data directory")
    parser.add_argument("--no-card-analysis", action="store_true")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--duration", type=float, default=3600)
    parser.add_argument("--idle-timeout", type=float, default=900)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 0.5 or args.duration <= 0 or args.idle_timeout <= 0:
        parser.error("interval >= 0.5; duration and idle-timeout must be positive")
    template = cv2.imdecode(np.fromfile(args.resource / "image" / "produce" / "health.png", dtype=np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        parser.error("health.png could not be read")
    skip_templates = [
        cv2.imdecode(np.fromfile(args.resource / "image" / "produce" / name, dtype=np.uint8), cv2.IMREAD_COLOR)
        for name in ("skip_round.png", "skip_round_1.png", "skip_round_2.png")
    ]
    if any(item is None for item in skip_templates):
        parser.error("skip-round templates could not be read")
    capture = DecisionCapture(
        args.output,
        {
            "mode": "adb_observer",
            "serial": args.serial,
            "adb": args.adb,
            "resource": str(args.resource.resolve()),
            "interval_seconds": args.interval,
            "coverage": "joined_mid_run",
            "actions": "not_observed",
            "detector": "health_and_skip_template_hint",
            "analysis_size": [720, 1280],
            "saved_frames": "720x1280",
        },
    )
    database = args.database or args.resource.parent.parent / "data/produce_skill_cards.json"
    if not args.no_card_analysis:
        if database.exists():
            capture.start_analysis(args.resource, database, database.with_name("skill_card_aliases.json"))
        else:
            capture.record("analysis_unavailable", error="skill_card_database_missing", database=str(database))
    print(f"采集目录: {capture.directory}", flush=True)
    (capture.directory / "observer.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "stop_file": str(capture.directory / "STOP"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    started = last_battle = time.monotonic()
    failures = 0
    was_battle = False
    recent_battle = 0
    samples = 0
    captured = 0
    reason = "duration_limit"
    try:
        while time.monotonic() - started < args.duration:
            if (capture.directory / "STOP").exists():
                reason = "stop_file"
                break
            if capture._failed:
                reason = "writer_failed"
                break
            tick = time.monotonic()
            try:
                image = screencap(args.adb, args.serial)
                score = health_score(image, template)
                skip = skip_score(image, skip_templates)
                battle = score is not None and score >= 0.8 and skip is not None and skip >= 0.8
                if battle:
                    recent_battle = time.monotonic()
                samples += 1
                if args.once or battle or time.monotonic() - recent_battle < 5 or was_battle or samples == 1:
                    capture.record(
                        "screen_observation",
                        image=image,
                        battle_hint=battle,
                        health_template_score=score,
                        skip_template_score=skip,
                        action=None,
                        game_action_confirmed=None,
                    )
                    captured += 1
                status = {
                    "pid": os.getpid(),
                    "samples": samples,
                    "frames_queued": captured,
                    "last_frame_time": time.time(),
                    "native_size": [image.shape[1], image.shape[0]],
                    "health_score": score,
                    "skip_score": skip,
                    "battle_hint": battle,
                    "status": "recording",
                }
                temporary = capture.directory / "observer_status.tmp"
                temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(capture.directory / "observer_status.json")
                if battle:
                    last_battle = time.monotonic()
                was_battle = battle
                failures = 0
                if args.once:
                    print(f"尺寸: {status['native_size']}; health_score={score}; battle_hint={battle}", flush=True)
                    reason = "once"
                    break
                if time.monotonic() - last_battle > args.idle_timeout:
                    reason = "no_battle_timeout"
                    break
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failures += 1
                capture.record("screenshot_error", error=str(exc), consecutive_failures=failures)
                if failures >= 3:
                    reason = "screenshot_failed"
                    break
            time.sleep(max(0, args.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    finally:
        capture.close(reason)
        status_path = capture.directory / "observer_status.json"
        if status_path.exists():
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status.update(status="stopped", stop_reason=reason)
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            from report_decision_capture import build_report

            build_report(capture.directory)
        except Exception as exc:
            print(f"报告生成失败: {exc}", file=sys.stderr, flush=True)
        print(f"采集结束: {reason}", flush=True)


if __name__ == "__main__":
    main()
