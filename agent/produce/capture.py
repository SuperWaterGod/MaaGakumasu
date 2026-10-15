"""Bounded, opt-in recording of observations and actions; never controls the game."""

import sys
import json
import time
import uuid
import queue
import shutil
import hashlib
import logging
import platform
import threading
import subprocess
from html import escape
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import version

ROOT = Path(__file__).resolve().parents[2]
LOGGER = logging.getLogger(__name__)


class DecisionCapture:
    def __init__(
        self,
        root: Path,
        metadata: dict,
        *,
        queue_size=32,
        max_bytes=2 * 1024**3,
        min_free_bytes=512 * 1024**2,
        analysis=None,
        console=None,
        console_details=True,
        reward_preview=True,
        reward_identity_cache=True,
    ):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        self.directory = root / self.run_id
        self.directory.mkdir(parents=True)
        (self.directory / "frames").mkdir()
        self._queue = queue.Queue(maxsize=queue_size)
        self._closed = threading.Event()
        self._failed = False
        self._seq = 0
        self._record_lock = threading.Lock()
        self.event_context = ({"session_id": metadata["session_id"], "segment_id": self.run_id}
                              if metadata.get("session_id") else {})
        self._dropped = 0
        self._written = 0
        self._bytes = 0
        self._hashes = {}
        self._max_bytes = max_bytes
        self._min_free_bytes = min_free_bytes
        self._console = console
        self._console_details = console_details
        self.reward_preview_enabled = reward_preview
        self.reward_identity_cache_enabled = reward_identity_cache
        self._console_offset = 0
        self._console_condition = threading.Condition()
        self._console_events = set()
        self._analysis_finished = False
        self._analysis_started = False
        self._analysis_process = None
        self._manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "recording",
            "metadata": metadata,
            "recorder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "python_version": platform.python_version(),
            "limits": {"queue_size": queue_size, "max_bytes": max_bytes, "min_free_bytes": min_free_bytes},
            "reward_preview_enabled": reward_preview,
            "reward_identity_cache_enabled": reward_identity_cache,
            "state_fields": {"card_identity": None, "buffs": None, "turn": None, "score": None, "stamina": None},
            "unknown_reason": ("整次培育的分段证据；完整性见培育会话清单。未知牌组、成本和动作确认不推断。"
                               if metadata.get("session_id") else
                               "结构化手牌、剩余回合、数值及可见 Buff 见独立后台分析结果；未确认字段保留未知。本目录不代表完整培育。"),
            "derived_state": "derived/card_identity/<analysis_id>/<frame>.json:battle_state",
            "frame_policy": "720x1280 portrait; unsupported aspect ratios retain original with explicit dimensions",
        }
        self._save_manifest()
        self._thread = threading.Thread(target=self._write_loop, name="decision-capture", daemon=True)
        self._thread.start()
        if analysis:
            self.start_analysis(**analysis)
        if console and analysis:
            threading.Thread(target=self._relay_console, name="card-analysis-console", daemon=True).start()

    def _emit_console(self, message, label="对局采集"):
        if self._console:
            try:
                lines = message.strip().splitlines()
                if self._console_details and len(lines) > 1:
                    # MFA reads one physical line per log item and renders it as
                    # Markdown/HTML. Keep the entire details block in that item.
                    title = escape(lines[0])
                    body = "<br/>".join(escape(line) for line in lines[1:])
                    self._console(f"<details><summary>[{label}] {title}</summary><p>{body}</p></details>")
                else:
                    for line in lines:
                        if line.strip():
                            self._console(f"[{label}] {line}")
                return True
            except Exception:
                LOGGER.exception("无法输出采集摘要，继续采集")
        return False

    @property
    def merged_console_enabled(self):
        return bool(self._console) and self._analysis_started and not self._analysis_finished

    def wait_for_console(self, event_id, timeout=3, cancelled=None):
        """Drain the final operation at a battle boundary, never wait indefinitely."""
        deadline = time.monotonic() + timeout
        with self._console_condition:
            while event_id not in self._console_events:
                if not event_id or not self.merged_console_enabled or self._failed:
                    return False
                if cancelled and cancelled():
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._console_condition.wait(min(remaining, 0.1))
            return True

    def _poll_console(self):
        path = self.directory / "analysis_console.jsonl"
        if not path.exists():
            return 0
        count = 0
        with path.open("rb") as stream:
            stream.seek(self._console_offset)
            for _ in range(20):
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                count += 1
                self._console_offset = stream.tell()
                try:
                    notice = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                emitted = self._emit_console(notice["message"], label="奖励采集" if notice.get("scope") == "reward" else "对局采集")
                with self._console_condition:
                    if emitted and notice.get("event_id"):
                        self._console_events.add(notice["event_id"])
                    if notice.get("finished"):
                        self._analysis_finished = True
                    self._console_condition.notify_all()
        return count

    def _relay_console(self):
        closed_since = None
        while not self._analysis_finished:
            try:
                count = self._poll_console()
                # A crashed worker may never write its final notice. Drain any
                # buffered notices before marking it unavailable.
                if self._analysis_process and self._analysis_process.poll() is not None and count < 20:
                    with self._console_condition:
                        self._analysis_finished = True
                        self._console_condition.notify_all()
            except OSError:
                pass
            if self._closed.is_set():
                closed_since = closed_since or time.monotonic()
                if time.monotonic() - closed_since > 20:
                    break
            time.sleep(0.3)

    def start_analysis(self, resource: Path, database: Path, aliases=None):
        try:
            command = [
                sys.executable,
                "-u",
                str(Path(__file__).with_name("analysis.py")),
                "--capture-dir",
                str(self.directory.resolve()),
                "--resource",
                str(resource.resolve()),
                "--database",
                str(database.resolve()),
                "--follow",
            ]
            if aliases and aliases.exists():
                command.extend(["--aliases", str(aliases.resolve())])
            with (self.directory / "analysis.log").open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            self._analysis_process = process
            self._analysis_started = True
            self.record("analysis_started", pid=process.pid, database=str(database), resource=str(resource))
            self._emit_console(f"已开启截图和后台手牌匹配，目录: {self.directory}")
        except Exception as exc:
            self.record("analysis_unavailable", error=str(exc))
            LOGGER.exception("卡牌后台分析启动失败，继续采集")
            self._emit_console(f"后台手牌匹配未启动: {exc}")

    @classmethod
    def from_config(cls, metadata: dict, console=None):
        try:
            config_path = ROOT / "config" / "decision_capture.json"
            if not config_path.exists():
                return None
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if config.get("enabled") is not True:
                return None

            metadata = dict(
                metadata,
                package_root=str(ROOT),
                maafw_package_version=version("maafw"),
                action_source_sha256=hashlib.sha256(b"".join(
                    (ROOT / name).read_bytes() for name in (
                        "agent/produce/events.py", "agent/produce/battle.py", "agent/produce/options.py", "agent/custom/action/produce.py"
                    )
                )).hexdigest(),
            )
            for interface_path in (ROOT / "interface.json", ROOT / "assets/interface.json"):
                if interface_path.exists():
                    metadata["resource_version"] = json.loads(interface_path.read_text(encoding="utf-8-sig")).get("version")
                    break
            package_data = ROOT / "data" if (ROOT / "data").is_dir() else ROOT / "assets/data"
            resource = ROOT / "resource/base" if (ROOT / "resource/base").is_dir() else ROOT / "assets/resource/base"
            database = package_data / "produce_skill_cards.json"
            analysis = None
            if config.get("match_cards", True) and database.exists():
                analysis = {"resource": resource, "database": database, "aliases": package_data / "skill_card_aliases.json"}
            return cls(
                ROOT / "debug" / "decision_capture",
                metadata,
                max_bytes=max(1024**2, int(config.get("max_bytes", 2 * 1024**3))),
                min_free_bytes=max(0, int(config.get("min_free_bytes", 512 * 1024**2))),
                analysis=analysis,
                console=console if config.get("console_summary", True) else None,
                console_details=config.get("console_details", True),
                reward_preview=config.get("reward_preview", True) is True,
                reward_identity_cache=config.get("reward_identity_cache", True) is True,
            )
        except Exception:
            LOGGER.exception("无法开启对局采集，继续原任务")
            return None

    def record(self, kind: str, *, image=None, **data):
        if self._closed.is_set() or self._failed:
            return None
        try:
            # Copy at enqueue time: Maa may reuse its screenshot buffer.
            snapshot = None if image is None else image.copy()
            with self._record_lock:
                if self._closed.is_set() or self._failed:
                    return None
                self._seq += 1
                event_id = f"{self.run_id}:{self._seq}"
                event = {
                    "schema_version": 1,
                    "run_id": self.run_id,
                    "event_id": event_id,
                    "seq": self._seq,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "monotonic_ns": time.monotonic_ns(),
                    "kind": kind,
                    **self.event_context,
                    **data,
                }
                self._queue.put_nowait((event, snapshot))
            return event_id
        except queue.Full:
            self._dropped += 1
        except Exception:
            self._dropped += 1
            LOGGER.exception("对局记录入队失败，继续原任务")
        return None

    def close(self, reason="completed", timeout=3):
        with self._record_lock:
            self._manifest["stop_reason"] = reason
            self._closed.set()
        self._thread.join(timeout=timeout)

    def _save_manifest(self):
        self._manifest.update(events_written=self._written, events_dropped=self._dropped, bytes_written=self._bytes)
        self._manifest["heartbeat_at"] = time.time()
        target = self.directory / "manifest.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _write_frame(self, image):
        from utils.image_io import bgr_image, encode_png, resize_image

        image = bgr_image(image)
        height, width = image.shape[:2]
        source_size = [width, height]
        if abs(width / height - 720 / 1280) <= 0.005:
            image = resize_image(image, 720, 1280)
        height, width = image.shape[:2]
        encoded = encode_png(image)
        digest = hashlib.sha256(encoded).hexdigest()
        if digest not in self._hashes:
            if self._bytes + len(encoded) > self._max_bytes:
                raise OSError("capture_size_limit")
            path = f"frames/{digest}.png"
            (self.directory / path).write_bytes(encoded)
            self._hashes[digest] = path
            self._bytes += len(encoded)
        return {
            "path": self._hashes[digest],
            "sha256": digest,
            "width": width,
            "height": height,
            "source_size": source_size,
            "resized": source_size != [width, height],
        }

    def _write_loop(self):
        try:
            last_manifest = time.monotonic()
            with (self.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1) as stream:
                while not self._closed.is_set() or not self._queue.empty():
                    try:
                        event, image = self._queue.get(timeout=0.2)
                    except queue.Empty:
                        if time.monotonic() - last_manifest >= 5:
                            self._save_manifest()
                            last_manifest = time.monotonic()
                        continue
                    try:
                        if shutil.disk_usage(self.directory).free < self._min_free_bytes:
                            raise OSError("capture_low_disk_space")
                        if image is not None:
                            event["frame"] = self._write_frame(image)
                        encoded = json.dumps(event, ensure_ascii=False) + "\n"
                        if self._bytes + len(encoded.encode("utf-8")) > self._max_bytes:
                            raise OSError("capture_size_limit")
                        stream.write(encoded)
                        self._bytes += len(encoded.encode("utf-8"))
                        self._written += 1
                        if self._written % 10 == 0:
                            self._save_manifest()
                            last_manifest = time.monotonic()
                    finally:
                        self._queue.task_done()
            self._manifest["status"] = "closed"
        except Exception as exc:
            self._failed = True
            self._dropped += 1 + self._queue.qsize()
            self._manifest.update(status="failed", error=str(exc))
            LOGGER.exception("对局采集停止，游戏任务继续")
        finally:
            self._manifest["ended_at"] = datetime.now(timezone.utc).isoformat()
            try:
                self._save_manifest()
            except Exception:
                LOGGER.exception("无法保存采集状态")
