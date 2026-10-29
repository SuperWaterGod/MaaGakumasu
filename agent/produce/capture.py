"""按配置和容量上限记录画面与动作，不控制游戏。"""

import os
import json
import time
import uuid
import shutil
import hashlib
import logging
import platform
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
        max_bytes=2 * 1024**3,
        min_free_bytes=512 * 1024**2,
        analysis=None,
        console=None,
        console_details=True,
        reward_preview=True,
        reward_identity_cache=True,
        save_screenshots=True,
        session_directory=None,
    ):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        self.directory = root / self.run_id
        if session_directory is not None:
            data = Path(session_directory) / "data"
            data.mkdir(parents=True, exist_ok=True)
            number = 1
            while True:
                self.directory = data / f"{number:02d}"
                try:
                    self.directory.mkdir()
                    break
                except FileExistsError:
                    number += 1
        else:
            self.directory.mkdir(parents=True)
        self.save_screenshots = save_screenshots
        self._frame_folder = "frames"
        if save_screenshots:
            (self.directory / self._frame_folder).mkdir()
        self._image_disabled = False
        self._closed = False
        self._frames = {}
        self._analyzing = False
        self.context = None
        self._failed = False
        self._seq = 0
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
        self._console_event = None
        self._analysis = None
        self._manifest = {
            "writer_pid": os.getpid(),
            "save_screenshots": save_screenshots,
            "schema_version": 1,
            "run_id": self.run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "recording",
            "metadata": metadata,
            "python_version": platform.python_version(),
            "limits": { "max_bytes": max_bytes, "min_free_bytes": min_free_bytes},
            "reward_preview_enabled": reward_preview,
            "reward_identity_cache_enabled": reward_identity_cache,
            "state_fields": {"card_identity": None, "buffs": None, "turn": None, "score": None, "stamina": None},
            "unknown_reason": ("整次培育的分段证据；完整性见培育会话清单。未知牌组、成本和动作确认不推断。"
                               if metadata.get("session_id") else
                               "结构化手牌、剩余回合、数值及可见 Buff 见识别结果；未确认字段保留未知。本目录不代表完整培育。"),
            "derived_state": "analysis/results.jsonl",
            "frame_policy": "720x1280 portrait; unsupported aspect ratios retain original with explicit dimensions",
        }
        self._save_manifest()
        self._events = (self.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        if analysis:
            self.start_analysis(**analysis)

    def _emit_console(self, message, label="对局采集"):
        if self._console:
            try:
                lines = message.strip().splitlines()
                if self._console_details and len(lines) > 1:
                    # MFA 将每条物理日志行作为独立条目渲染，
                    # 因此完整的 Markdown/HTML 折叠块必须保留在同一日志行中。
                    from .console import format_operation
                    self._console(format_operation(lines[0], lines[1:]))
                else:
                    if lines:
                        self._console(lines[0])
                return True
            except Exception:
                LOGGER.exception("无法输出采集摘要，继续采集")
        return False

    @property
    def merged_console_enabled(self):
        return bool(self._console) and self._analysis is not None

    def console_emitted(self, event_id):
        """同步分析已完成，不再轮询或等待后台通知。"""
        return event_id == self._console_event

    def start_analysis(self, resource: Path, database: Path, aliases=None, context=None):
        if context is None:
            return
        from .analysis import analyze_stream

        def notice(message, **extra):
            if self._emit_console(message) and extra.get("event_id"):
                self._console_event = extra["event_id"]

        try:
            self._analysis = analyze_stream(self.directory, resource, database, aliases,
                                            context=lambda: self.context, emit=notice)
            next(self._analysis)
        except Exception:
            self._analysis = None
            LOGGER.exception("记录识别初始化失败，继续培育")

    @classmethod
    def from_config(cls, metadata: dict, console=None, *, session_directory=None, save_screenshots=None, context=None):
        try:
            config_path = ROOT / "config" / "decision_capture.json"
            config = {}
            try:
                if config_path.exists():
                    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if not isinstance(config, dict):
                    config = {}
            except (OSError, ValueError):
                LOGGER.warning("采集配置不可读，培育使用默认记录设置")
            if session_directory is None and config.get("enabled") is not True:
                return None

            metadata = dict(
                metadata,
                package_root=str(ROOT),
                maafw_package_version=version("maafw"),
            )
            for interface_path in (ROOT / "interface.json", ROOT / "assets/interface.json"):
                if interface_path.exists():
                    metadata["resource_version"] = json.loads(interface_path.read_text(encoding="utf-8-sig")).get("version")
                    break
            package_data = ROOT / "data" if (ROOT / "data").is_dir() else ROOT / "assets/data"
            resource = ROOT / "resource/base" if (ROOT / "resource/base").is_dir() else ROOT / "assets/resource/base"
            database = package_data / "produce_skill_cards.json"
            if database.exists():
                items = package_data / "produce_reward_items.json"
                metadata["reward_cache"] = {
                    "version": 3, "layout": "720x1280_reward_v1",
                    "catalog_hashes": {"skill_cards": hashlib.sha256(database.read_bytes()).hexdigest(),
                                       "reward_items": hashlib.sha256(items.read_bytes()).hexdigest() if items.exists() else None},
                    "language_layout": (metadata.get("reward_language_layout") or {}).get("recognition"),
                }
            analysis = None
            if config.get("match_cards", True) and database.exists():
                analysis = {"resource": resource, "database": database, "aliases": package_data / "skill_card_aliases.json", "context": context}
            def limit(name, default, minimum):
                try:
                    return max(minimum, int(config.get(name, default)))
                except (ValueError, TypeError, OverflowError):
                    return default
            return cls(
                ROOT / "debug" / "decision_capture",
                metadata,
                session_directory=session_directory,
                save_screenshots=True if save_screenshots is None else save_screenshots,
                max_bytes=limit("max_bytes", 2 * 1024**3, 1024**2),
                min_free_bytes=limit("min_free_bytes", 512 * 1024**2, 0),
                analysis=analysis,
                console=console if config.get("console_summary", True) else None,
                console_details=config.get("console_details", True),
                reward_preview=config.get("reward_preview", True) is True,
                reward_identity_cache=config.get("reward_identity_cache", True) is True,
            )
        except Exception:
            LOGGER.exception("无法开启对局采集，继续原任务")
            return None

    def record(self, kind: str, *, image=None, context=None, **data):
        if self._closed or self._failed or self._analyzing:
            return None
        try:
            self._seq += 1
            event_id = f"{self.run_id}:{self._seq}"
            event = {"schema_version": 1, "run_id": self.run_id, "event_id": event_id, "seq": self._seq,
                     "time": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns(),
                     "kind": kind, **self.event_context, **data}
            if image is not None:
                height, width = image.shape[:2]
                event["frame"] = {"retention": "memory", "sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                                  "width": width, "height": height, "source_size": [width, height], "resized": False}
                if self.save_screenshots and not self._image_disabled:
                    try:
                        if shutil.disk_usage(self.directory).free < self._min_free_bytes:
                            raise OSError("capture_low_disk_space")
                        event["frame"] = self._write_frame(image)
                    except OSError as exc:
                        self._image_disabled = True
                        self._manifest["image_error"] = str(exc)
                        event["frame_missing_reason"] = str(exc)
                        self._emit_console("截图保存停止，文字记录与识别继续：" + str(exc))
                elif self._image_disabled:
                    event["frame_missing_reason"] = self._manifest.get("image_error")
            reference = event.get("frame_reference")
            if reference in self._frames:
                event["frame"] = self._frames[reference]
            if "frame" in event:
                self._frames[event_id] = event["frame"]
                if len(self._frames) > 128:
                    self._frames.pop(next(iter(self._frames)))
            self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._written += 1
            if self._analysis:
                self._analyzing = True
                self.context = context
                try:
                    self._analysis.send((event, image))
                except Exception:
                    self._analysis = None
                    LOGGER.exception("记录识别失败，继续培育")
                finally:
                    self.context = None
                    self._analyzing = False
            if self._written % 10 == 0:
                self._save_manifest()
            return event_id
        except Exception:
            self._dropped += 1
            LOGGER.exception("记录保存失败，继续培育")
            return None

    def close(self, reason="completed"):
        if self._closed:
            return
        self._closed = True
        self._events.close()
        if self._analysis:
            try:
                self._analysis.send(None)
            except StopIteration:
                pass
            except Exception:
                LOGGER.exception("记录识别收尾失败")
            self._analysis = None
        self._manifest.update(status="closed", stop_reason=reason, ended_at=datetime.now(timezone.utc).isoformat())
        self._save_manifest()

    def _save_manifest(self):
        self._manifest.update(events_written=self._written, events_dropped=self._dropped, bytes_written=self._bytes)
        self._manifest["heartbeat_at"] = time.time()
        target = self.directory / "manifest.json"
        from .reporting import atomic_json
        try:
            atomic_json(target, self._manifest)
            self._manifest.pop("manifest_write_error", None)
        except PermissionError as exc:
            # Windows 的短暂共享锁不能终止文字采集；后续心跳继续尝试。
            if not self._manifest.get("manifest_write_error"):
                self._emit_console("记录清单暂时被占用，文字记录继续；稍后重试保存")
            self._manifest["manifest_write_error"] = str(exc)

    def _write_frame(self, image):
        from utils.image_io import bgr_image, encode_png

        image = bgr_image(image)
        height, width = image.shape[:2]
        source_size = [width, height]
        encoded = encode_png(image)
        digest = hashlib.sha256(encoded).hexdigest()
        if digest not in self._hashes:
            if self._bytes + len(encoded) > self._max_bytes:
                raise OSError("capture_size_limit")
            path = f"{self._frame_folder}/frame_{len(self._hashes) + 1:06d}.png"
            (self.directory / path).write_bytes(encoded)
            self._hashes[digest] = path
            self._bytes += len(encoded)
        return {
            "retention": "permanent",
            "path": self._hashes[digest],
            "sha256": digest,
            "width": width,
            "height": height,
            "source_size": source_size,
            "resized": source_size != [width, height],
        }
