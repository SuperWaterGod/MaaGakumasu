"""培育记录生命周期与缓存回归；仅使用合成画面，不连接设备。"""

import os
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]

from produce.state import read_produce_health, read_produce_attributes
from produce.capture import DecisionCapture
from produce.rewards import RewardIdentityCache, cache_write_lock, cache_for_capture
from produce.reporting import process_alive, segment_directory


class RecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from produce.analysis import OfflineVision
        cls.vision = OfflineVision(ROOT / "assets/resource/base")

    def identity_cache(self, *args):
        return RewardIdentityCache(*args, recognize=self.vision.recognize)

    def setUp(self):
        root = ROOT.parent / "MaaGakumasuLab/debug/research/produce/tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="recording_", dir=root)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.image = np.zeros((1280, 720, 3), dtype=np.uint8)
        self.image[840:900, 100:170] = [80, 160, 220]

    def capture(self, **kwargs):
        result = DecisionCapture(self.directory, {"session_id": "session"}, min_free_bytes=0, **kwargs)
        self.addCleanup(result.close)
        return result

    def test_reward_layout_recovers_split_selected_frame(self):
        from produce.rewards import reward_layout
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        for x in (150, 290, 430):
            image[820:948, x:x + 128] = 100
            image[824:944, x + 4:x + 124] = 0
        image[820:824, 340:360] = 0
        image[944:948, 340:360] = 0
        image[807:818, 300:408] = [0, 150, 255]
        image[950:961, 300:408] = [0, 150, 255]
        layout = reward_layout(image)
        self.assertEqual(layout["status"], "ready")
        self.assertEqual(layout["selected_index"], 1)
        self.assertEqual([item["box"][0] for item in layout["candidates"]], [150, 290, 430])
        # 不能仅凭两个边框推断中间一定存在候选。
        image[800:970, 275:420] = 0
        self.assertNotEqual(reward_layout(image)["status"], "ready")

    def test_reward_layout_handles_one_sided_border_loss(self):
        from produce.rewards import reward_layout
        image = np.zeros((1280, 720, 3), dtype=np.uint8)
        for x, width in ((155, 129), (297, 125), (432, 129)):
            image[820:948, x:x + width] = 100
            image[824:944, x + 4:x + width - 4] = 0
        image[807:818, 165:274] = [0, 150, 255]
        image[950:961, 165:274] = [0, 150, 255]
        layout = reward_layout(image)
        self.assertEqual(layout["status"], "ready")
        self.assertEqual(layout["selected_index"], 0)
        self.assertEqual(len(layout["candidates"]), 3)

    def test_reward_layout_four_candidates_and_selection_restore(self):
        from produce.rewards import reward_layout, verify_reward_layout
        for selected in range(4):
            for damaged in (False, True):
                with self.subTest(selected=selected, damaged=damaged):
                    image = np.zeros((1280, 720, 3), dtype=np.uint8)
                    for index in range(4):
                        x = 90 + index * 140
                        image[820:948, x:x + 128] = 100
                        image[824:944, x + 4:x + 124] = 0
                    x = 90 + selected * 140
                    if damaged:
                        image[820:824, x + 50:x + 70] = 0
                        image[944:948, x + 50:x + 70] = 0
                    image[807:818, x + 10:x + 118] = [0, 150, 255]
                    image[950:961, x + 10:x + 118] = [0, 150, 255]
                    layout = reward_layout(image)
                    self.assertEqual(layout["status"], "ready")
                    self.assertEqual(len(layout["candidates"]), 4)
                    self.assertEqual(layout["selected_index"], selected)
                    for target in range(4):
                        changed = image.copy()
                        changed[800:820] = 0
                        changed[948:970] = 0
                        tx = 90 + target * 140
                        changed[807:818, tx + 10:tx + 118] = [0, 150, 255]
                        changed[950:961, tx + 10:tx + 118] = [0, 150, 255]
                        verified = verify_reward_layout(changed, image, layout)
                        self.assertEqual(verified["status"], "ready")
                        self.assertEqual(verified["selected_index"], target)
                    restored = verify_reward_layout(image, image, layout)
                    self.assertEqual(restored["selected_index"], selected)

    def test_reward_failed_layout_keeps_folded_explanation(self):
        from maa.library import Library
        with patch.object(Library, "_is_agent_server", False), \
                patch("maa.agent.agent_server.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls), \
                patch("maa.agent.agent_server.AgentServer.custom_recognition", side_effect=lambda name: lambda cls: cls):
            from custom.action.produce import ProduceRewardConfirm
        with patch("custom.action.produce.logger") as logger:
            ProduceRewardConfirm.log_receive(True, {"policy_node": "ProduceChooseFirst"}, "ambiguous_spacing")
        logger.info.assert_called_once()
        message = logger.info.call_args.args[0]
        self.assertIn("<details>", message)
        self.assertIn("候选图标间距未确认", message)
        self.assertIn("候选名称：未识别", message)

    def test_segments_hidden_under_data(self):
        directory = self.directory / "session"
        first = DecisionCapture(self.directory, {}, session_directory=directory, min_free_bytes=0)
        first.close()
        second = DecisionCapture(self.directory, {}, session_directory=directory, min_free_bytes=0)
        second.close()
        self.assertEqual(first.directory, directory / "data/01")
        self.assertEqual(second.directory, directory / "data/02")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(segment_directory(directory, {"relative_path": "."}), directory)

    def test_atomic_write_retries_windows_sharing_violation(self):
        from produce.reporting import atomic_json
        target = self.directory / "atomic.json"
        original = Path.replace
        attempts = []

        def replace(path, destination):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError("sharing violation")
            return original(path, destination)

        with patch.object(Path, "replace", replace), patch("produce.reporting.time.sleep"):
            atomic_json(target, {"saved": True})
        self.assertEqual(json.loads(target.read_text()), {"saved": True})
        self.assertEqual(len(attempts), 3)
        self.assertFalse(list(self.directory.glob("*.tmp")))

    def test_manifest_contention_keeps_recording_text(self):
        capture = self.capture()
        with patch("produce.reporting.atomic_json", side_effect=PermissionError("busy")):
            for index in range(12):
                capture.record("synthetic", index=index)
            self.assertFalse(capture._failed)
        capture.record("after_recovery")
        capture.close()
        manifest = json.loads((capture.directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "closed")
        self.assertEqual(manifest["events_written"], 13)

    def test_console_single_root_and_readable_fields(self):
        from produce.console import format_operation, operation_detail_rows
        rows = operation_detail_rows({"actual_point": [100, 200], "framework_action_succeeded": True,
                                      "attributes": {"Vo": {"status": "observed", "value": {"current": 100, "max": 2000}}, "Da": {"status": "unknown", "value": None}}})
        text = format_operation("属性选择－点击无属性选项", rows)
        self.assertTrue(text.startswith("<details><summary>属性选择－点击无属性选项</summary>"))
        self.assertTrue(text.endswith("</p></details>"))
        self.assertNotIn("<ul>", text)
        self.assertNotIn("<li>", text)
        self.assertNotIn("\n", text)
        self.assertIn("<br/>", text)
        self.assertNotIn("x=100", text)
        self.assertIn("声乐：100/2000", text)
        self.assertIn("舞蹈：未知", text)
        self.assertNotIn("null", text)
        self.assertNotIn("True", text)
        self.assertIn("&lt;", format_operation("<标题>", ["<内容>"]))

    def test_default_console_preserves_folded_details(self):
        notices = []
        capture = self.capture(console=notices.append)
        capture._emit_console("工作选择－点击第3项\n体力：42/42\n点击坐标：100, 1000")
        capture.close()
        self.assertEqual(len(notices), 1)
        self.assertIn("<summary>工作选择－点击第3项</summary>", notices[0])
        self.assertIn("体力：42/42<br/>点击坐标：100, 1000", notices[0])

    def test_custom_operation_emits_details_at_info_once(self):
        from produce.console import log_operation
        with patch("utils.logger") as logger:
            log_operation("路线选择", "已选择活动", {"candidates": {"活动": 10, "休息": 5}})
        logger.info.assert_called_once()
        self.assertIn("<summary>路线选择 - 已选择活动</summary>", logger.info.call_args.args[0])
        self.assertIn("活动：10", logger.info.call_args.args[0])
        logger.debug.assert_not_called()

    def test_ready_report_notification_link_and_once(self):
        from produce.reporting import notify_ready_reports
        directory = self.directory / "session"
        (directory / "data").mkdir(parents=True)
        (directory / "data/session.json").write_text('{"schema_version":4}')
        (directory / "data/report_status.json").write_text('{"status":"completed"}')
        (directory / "report.md").write_text("report")
        messages = []
        notify_ready_reports(self.directory, messages.append)
        notify_ready_reports(self.directory, messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn((directory / "report.md").as_posix(), messages[0])
        self.assertIn("打开培育回放", messages[0])

    def test_reward_console_only_lists_identity_and_selection(self):
        from produce.console import RewardConsole
        console = RewardConsole()
        console.observe({"kind": "reward_page_start", "event_id": "page", "selected_candidate_id": "a",
                         "layout": {"candidates": [{"candidate_id": "a", "box": [0, 0, 1, 1]}]}})
        console.observe({"kind": "reward_candidate_preview", "reward_page_id": "page", "candidate": {"candidate_id": "a"}},
                        {"identity_verified": True, "identity": {"status": "matched", "database_name": "合成卡"},
                         "effects": {"visible_text": ["不应展示的效果"]}})
        result = console.observe({"kind": "reward_receive_input", "reward_page_id": "page", "event_id": "receive",
                                  "framework_action_succeeded": True})
        self.assertEqual(result["message"].splitlines(), ["奖励选择 - 已点击领取：合成卡", "1｜合成卡（已选择）",
                                                       "模板匹配：0 项｜OCR 匹配：0 项｜新增缓存：0 项"])

    def test_cache_tolerates_brightness_deduplicates_and_rejects_conflicts(self):
        cache = self.identity_cache(self.directory / "tolerant_cache", {})
        image = np.random.default_rng(42).integers(20, 220, size=self.image.shape, dtype=np.uint8)
        event = {"event_id": "a", "candidate": {"box": [55, 790, 150, 150]}}
        observation = {"detail_verified": True, "identity": {"status": "matched", "catalog_id": "a",
                       "database_name": "合成甲", "item_kind": "skill_card"}}
        self.assertEqual(cache.learn(image, event, observation), "stored")
        shifted_brightness = image + 10
        self.assertEqual(cache.lookup(shifted_brightness, event["candidate"]["box"])["catalog_id"], "a")
        self.assertEqual(cache.learn(shifted_brightness, event, observation), "reused")
        # OCR 已确认同一身份，图像差异不应再产生第二份缓存。
        changed = np.flip(image, axis=1).copy()
        self.assertEqual(cache.learn(changed, event, observation), "reused")
        self.assertEqual(len(list((cache.directory / "icons").glob("*.png"))), 1)
        observation["identity"]["catalog_id"] = "b"
        cache.learn(image, event, observation)
        self.assertIsNone(cache.lookup(image, event["candidate"]["box"]))

    def test_status_read_retries_temporary_access_denial(self):
        from produce.reporting import read_json
        with patch.object(Path, "read_text", side_effect=[PermissionError("busy"), '{"status":"recording"}']), \
                patch("produce.reporting.time.sleep") as sleep:
            self.assertEqual(read_json(self.directory / "manifest.json"), {"status": "recording"})
        sleep.assert_called_once()

    def test_icon_crop_keeps_original_pixels(self):
        from produce.rewards import icon_image
        crop = icon_image(self.image, [55, 790, 130, 132])
        self.assertEqual(crop.shape, (124, 122, 3))
        np.testing.assert_array_equal(crop, self.image[794:918, 59:181])

    def test_saved_frame_keeps_framework_dimensions(self):
        from utils.image_io import read_image
        capture = self.capture(save_screenshots=True)
        original = np.zeros((640, 360, 3), dtype=np.uint8)
        capture.record("frame", image=original)
        capture.close()
        event = json.loads((capture.directory / "events.jsonl").read_text(encoding="utf-8"))
        self.assertFalse(event["frame"]["resized"])
        np.testing.assert_array_equal(read_image(capture.directory / event["frame"]["path"]), original)

    def test_battle_reuses_next_before_frame_without_second_encoding(self):
        from maa.library import Library
        with patch.object(Library, "_is_agent_server", False), \
                patch("maa.agent.agent_server.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls):
            from produce.battle import ProduceCardsAuto
        capture = self.capture(save_screenshots=True)
        action = ProduceCardsAuto()
        action._capture = capture
        action._battle_id = "battle"
        with patch.object(capture, "_write_frame", wraps=capture._write_frame) as write:
            action._begin_decision("play_card", self.image)
            first = action._active_decision
            action._finish_decision(True, selection_click_seconds=2)
            next_image = self.image.copy()
            next_image[800:810] = 100
            action._begin_decision("play_card", next_image)
            second = action._active_decision
            action._finish_decision(False, selection_click_seconds=3)
            action._flush_decision()
            capture.close()
        self.assertEqual(write.call_count, 2)
        events = [json.loads(line) for line in (capture.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        by_id = {event["event_id"]: event for event in events}
        after = {event["decision_id"]: event for event in events if event["kind"] == "decision_after"}
        self.assertEqual(after[first]["frame_reference"], second)
        self.assertEqual(after[first]["frame"], by_id[second]["frame"])
        self.assertNotIn("frame", after[second])
        self.assertEqual(after[first]["selection_click_seconds"], 2)

        from produce.analysis import OfflineVision, analyze_capture
        with patch.object(OfflineVision, "analyze", return_value={"cards": [], "battle_state": {}}) as analyze:
            analyze_capture(capture.directory, ROOT / "assets/resource/base", ROOT / "assets/data/produce_skill_cards.json",
                            ROOT / "assets/data/skill_card_aliases.json", False, None)
        self.assertEqual(analyze.call_count, 2)
        results = [json.loads(line) for line in (capture.directory / "analysis/results.jsonl").read_text(encoding="utf-8").splitlines()]
        reused = next(result for result in results if result["event_id"] == after[first]["event_id"])
        self.assertEqual(reused["result_ref"], second)

    def test_session_name_uses_full_date_and_minute(self):
        from produce.sessions import session_directory_name
        name = session_directory_name({"configured_idol": {"idol_name": "偶像", "song_name": "歌曲"}})
        self.assertRegex(name, r"^\d{8}_\d{4}_偶像_歌曲$")

    def test_reward_reader_never_requests_effect_or_cost_ocr(self):
        from collections import OrderedDict

        from produce.rewards import DETAIL_ROIS, RewardReader
        reader = RewardReader.__new__(RewardReader)
        reader.catalog = SimpleNamespace(skills=None)
        reader.icons = None
        reader.details = OrderedDict()
        reader.counts = {"name_ocr": 0}
        event = {"event_id": "preview", "detail_refreshed": True, "selected_highlight_verified": True,
                 "customization_hint": True}
        with patch("produce.rewards.reward_preview_observation", return_value={"reading": {}}):
            calls = []
            reader.read(self.image, event, lambda image, roi: calls.append(roi) or [])
        self.assertEqual(calls, [DETAIL_ROIS["name"]])

    def test_battle_title_has_number_card_and_elapsed_time(self):
        from produce.console import operation_console
        message, _ = operation_console(2, {"operation": "play_card", "monotonic_ns": 1_000_000_000,
                                           "selected_box": [0, 0, 1, 1]},
                                      {"cards": [{"box": [0, 0, 1, 1], "identity": {"status": "unknown", "observed_name": "合成卡"}}]},
                                      {"monotonic_ns": 7_000_000_000, "selection_click_seconds": 2}, {})
        self.assertEqual(message.splitlines()[0], "战斗 - 出牌#2[合成卡 [未知]] 2s")

    def test_screenshots_disabled_never_writes_temporary_frames(self):
        capture = self.capture(save_screenshots=False)
        capture.record("decision_before", image=self.image)
        self.assertFalse((capture.directory / "temporary_frames").exists())
        capture.close()
        self.assertFalse((capture.directory / "temporary_frames").exists())
        event = json.loads((capture.directory / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(event["frame"]["retention"], "memory")
        self.assertNotIn("path", event["frame"])
        self.assertEqual(list(capture.directory.rglob("*.png")), [])

    def test_inline_analysis_uses_context_and_memory_without_workers(self):
        from produce.analysis import OfflineVision

        notices, calls = [], []
        capture = self.capture(save_screenshots=False, console=notices.append)

        def recognize(kind, param, image):
            calls.append(image)
            # 内部识别回调不能再次记录或触发识别。
            self.assertIsNone(capture.record("nested_callback"))
            return SimpleNamespace(all_results=[])

        context = SimpleNamespace(run_recognition_direct=recognize)
        with patch("threading.Thread", side_effect=AssertionError("unexpected thread")), \
                patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
            capture.start_analysis(ROOT / "assets/resource/base", ROOT / "assets/data/produce_skill_cards.json", context=context)
            self.assertTrue(capture.merged_console_enabled)
            event_id = capture.record("route_before", image=self.image, context=context)
            self.assertTrue(calls)
            self.assertTrue(np.array_equal(calls[0], self.image))
            rows = [json.loads(line) for line in (capture.directory / "analysis/results.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["event_id"], event_id)
            self.assertNotIn("error", rows[0].get("observation", {}))
            capture.close()
        self.assertFalse((capture.directory / "temporary_frames").exists())
        self.assertEqual(list(capture.directory.rglob("*.png")), [])
        self.assertFalse(hasattr(OfflineVision(None, context), "tasker"))

    def test_nested_callback_context_is_not_retained_for_reward_ocr(self):
        from produce.sessions import ProduceSessions

        capture = self.capture(save_screenshots=False)
        capture.start_analysis(ROOT / "assets/resource/base", ROOT / "assets/data/produce_skill_cards.json",
                               context=SimpleNamespace())
        manager = ProduceSessions()
        manager.active["test"] = {"capture": capture}
        expired = SimpleNamespace(tasker=object())
        with patch.object(manager, "key", return_value="test"):
            self.assertIsNotNone(manager.get(expired))
        self.assertIsNone(capture.context)
        calls = []
        current = SimpleNamespace(run_recognition_direct=lambda kind, param, image:
                                  calls.append(kind) or SimpleNamespace(all_results=[]))
        capture.record("reward_candidate_preview", image=self.image, context=current,
                       candidate={"candidate_id": "reward_1", "box": [150, 820, 128, 128]},
                       detail_refreshed=True, selected_highlight_verified=True)
        self.assertTrue(calls)
        self.assertIsNone(capture.context)
        rows = [json.loads(line) for line in (capture.directory / "analysis/results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("error", rows[0]["observation"])
        self.assertEqual(rows[0]["observation"]["reading"]["method"], "name_then_catalog")

    def test_permanent_frames_deduplicated_and_preserved(self):
        capture = self.capture(save_screenshots=True)
        capture.record("decision_before", image=self.image)
        capture.record("decision_after", image=self.image)
        capture.close()
        self.assertEqual(len(list((capture.directory / "frames").glob("*.png"))), 1)
        self.assertEqual(len((capture.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()), 2)

    def test_image_limit_does_not_stop_text(self):
        capture = self.capture(max_bytes=1)
        capture.record("decision_before", image=self.image)
        capture.record("decision_after", framework_action_succeeded=True)
        capture.close()
        events = [json.loads(line) for line in (capture.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["frame_missing_reason"], "capture_size_limit")
        self.assertEqual(json.loads((capture.directory / "manifest.json").read_text(encoding="utf-8"))["status"], "closed")

    def test_recording_defaults_without_configuration(self):
        with patch("produce.capture.ROOT", self.directory), patch.object(DecisionCapture, "start_analysis"):
            # 版本指纹需要的最小包布局，不写真实配置。
            for name in ("events.py", "battle.py", "options.py"):
                path = self.directory / "agent/produce" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")
            path = self.directory / "agent/custom/action/produce.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
            self.assertIsNone(DecisionCapture.from_config({}))
            capture = DecisionCapture.from_config({"session_id": "session"}, session_directory=self.directory / "session",
                                                  save_screenshots=False)
            self.assertIsNotNone(capture)
            capture.close()

    def test_path_resolution_and_escape(self):
        directory = self.directory / "produce_sessions/display_name"
        directory.mkdir(parents=True)
        with self.assertRaises(KeyError):
            segment_directory(directory, {"run_id": "old"})
        self.assertEqual(segment_directory(directory, {"run_id": "new", "relative_path": "segments/new"}),
                         directory / "segments/new")
        with self.assertRaises(ValueError):
            segment_directory(directory, {"run_id": "bad", "relative_path": "../../escape"})

    def test_process_probe_never_terminates_current_process(self):
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(None))

    def test_shared_cache_survives_another_instance_and_checks_version(self):
        hashes = {"skill_cards": "s", "reward_items": "i"}
        cache = self.identity_cache(self.directory / "cache", hashes)
        event = {"event_id": "first:1", "candidate": {"box": [55, 790, 150, 150]}, "frame": {"sha256": "frame"}}
        observation = {"detail_verified": True, "identity": {"status": "matched", "needs_full_ocr": False,
                       "effective_effect_dsl": "effect", "catalog_id": "card-1", "database_name": "合成卡", "item_kind": "skill_card"}}
        cache.learn(self.image, event, observation)
        second = self.identity_cache(cache.directory, hashes)
        self.assertEqual(second.lookup(self.image, event["candidate"]["box"])["catalog_id"], "card-1")
        self.assertIsNone(self.identity_cache(cache.directory, {"skill_cards": "changed"}).lookup(self.image, event["candidate"]["box"]))
        modified = self.image.copy()
        modified[810:835, 70:100] = 255
        self.assertIsNone(second.lookup(modified, event["candidate"]["box"]))
        icon = next((cache.directory / "icons").glob("*.png"))
        icon.write_bytes(b"corrupt")
        self.assertIsNone(self.identity_cache(cache.directory, hashes).lookup(self.image, event["candidate"]["box"]))

    def test_shared_cache_scope_independent_of_session(self):
        scope = {"version": 2, "layout": "synthetic", "language_layout": "ja", "catalog_hashes": {"skill_cards": "s"}}
        directories = [self.directory / "first", self.directory / "second"]
        for directory in directories:
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps({"metadata": {"reward_cache": scope}}))
        self.assertEqual(cache_for_capture(directories[0]).directory, cache_for_capture(directories[1]).directory)
        scope["language_layout"] = "zh"
        (directories[1] / "manifest.json").write_text(json.dumps({"metadata": {"reward_cache": scope}}))
        self.assertEqual(cache_for_capture(directories[0]).directory, cache_for_capture(directories[1]).directory)
        self.assertNotEqual(cache_for_capture(directories[0]).scope, cache_for_capture(directories[1]).scope)

    def test_cache_scope_change_does_not_reuse_identity(self):
        cache = self.identity_cache(self.directory / "scope_cache", {}, {"language": "ja"})
        event = {"event_id": "first:1", "candidate": {"box": [55, 790, 150, 150]}}
        observation = {"detail_verified": True, "identity": {"status": "matched", "needs_full_ocr": False,
                       "effective_effect_dsl": "effect", "catalog_id": "card-1", "database_name": "合成卡", "item_kind": "skill_card"}}
        cache.learn(self.image, event, observation)
        changed = self.identity_cache(cache.directory, {}, {"language": "zh"})
        self.assertIsNone(changed.lookup(self.image, event["candidate"]["box"]))

    def test_cache_lock_is_released(self):
        path = self.directory / "cache.lock"
        with cache_write_lock(path) as first:
            self.assertTrue(first)
            with cache_write_lock(path) as second:
                self.assertFalse(second)
        with cache_write_lock(path) as third:
            self.assertTrue(third)

    def test_health_zero_and_unknown(self):
        context = SimpleNamespace(run_recognition=lambda *a, **k: SimpleNamespace(hit=True, best_result=SimpleNamespace(text="0/34")))
        self.assertEqual(read_produce_health(context, self.image)["current"], 0)
        context.run_recognition = lambda *a, **k: None
        self.assertIsNone(read_produce_health(context, self.image))

    def test_partial_attributes_preserve_legacy_input(self):
        results = iter([SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text="0"), SimpleNamespace(text="/1500")]), None, None])
        context = SimpleNamespace(run_recognition=lambda *a, **k: next(results))
        legacy, observed = read_produce_attributes(context, self.image, [[0, 0, 1, 1]] * 3)
        self.assertEqual(legacy, {"Vo": 0, "Da": 0, "Vi": 0, "max": 1500})
        self.assertEqual(observed["Vo"]["value"]["current"], 0)
        self.assertIsNone(observed["Da"]["value"])

    def test_route_entry_details_do_not_block_real_candidate_shapes(self):
        from contextlib import ExitStack

        with patch("maa.agent.agent_server.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls):
            from produce.events import ProduceChooseEventAuto, ProduceChooseNIAEventAuto
        context = SimpleNamespace(get_node_data=lambda name: {"enabled": False})
        candidates = [{"Vo": [190, 1000, 1, 1], "SP": False},
                      {"Da": [360, 1000, 1, 1], "SP": True}, {"Vi": [530, 1000, 1, 1], "SP": False}]
        for cls in (ProduceChooseEventAuto, ProduceChooseNIAEventAuto):
            with self.subTest(action=cls.__name__), ExitStack() as stack:
                action = cls()
                values = {"_get_preference": {"first": "Da", "second": "Vo"}, "_get_screenshot": self.image,
                          "_get_health": {"current": 34, "max": 34, "ratio": 1.0}, "_get_current_points": 110,
                          "_get_current_score": {"Vo": 100, "Da": 100, "Vi": 100, "max": 2000},
                          "_get_available_events": candidates}
                for name, value in values.items():
                    stack.enter_context(patch.object(action, name, return_value=value))
                stack.enter_context(patch("produce.events.SESSIONS.get", return_value=None))
                log = stack.enter_context(patch("produce.events.log_operation"))
                logger = stack.enter_context(patch("produce.events.logger"))
                execute = stack.enter_context(patch.object(action, "_execute_event", return_value=True))
                self.assertTrue(action.run(context, SimpleNamespace()))
                execute.assert_called_once()
                self.assertEqual(execute.call_args.args[1]["name"], "Da_SP")
                details = logger.info.call_args.args[0]
                self.assertIn("路线选择 - 进入事件页面", details)
                self.assertIn("1｜声乐", details)
                self.assertIn("2｜→ 舞蹈 SP课程", details)
                self.assertIn("3｜形象", details)
                self.assertEqual(log.call_count, 1)

    def test_attribute_retry_reads_number_only_with_verified_limit(self):
        def result(*texts):
            return SimpleNamespace(hit=True, filtered_results=[SimpleNamespace(text=t, score=.99) for t in texts])
        for text, expected in (("295", 295), ("0", 0), ("3001", None), ("abc", None)):
            with self.subTest(text=text):
                replies = iter([result("/2000"), result(text), result("169", "/2000"), result("290", "/2000")])
                calls = []
                def recognize(name, image, pipeline_override):
                    calls.append(pipeline_override)
                    return next(replies)
                _, observed = read_produce_attributes(SimpleNamespace(run_recognition=recognize), self.image,
                                                       [[0, 0, 100, 80]] * 3, [[10, 10, 80, 30]] * 3)
                value = observed["Vo"]["value"]
                self.assertEqual(value["current"] if value else None, expected)
                self.assertEqual(len(calls), 4)
                self.assertTrue(calls[1]["ProduceRecognitionScore"]["only_rec"])

    def test_work_choice_preserves_legacy_health_fallback(self):
        with patch("maa.agent.agent_server.AgentServer.custom_action", side_effect=lambda name: lambda cls: cls):
            from produce.options import ProduceChooseWorkAuto
        for health, expected in ((11, [100, 880]), (0, [100, 1000]), (None, [100, 1000])):
            clicks = []
            controller = SimpleNamespace(
                post_screencap=lambda: SimpleNamespace(wait=lambda: SimpleNamespace(get=lambda: self.image)),
                post_click=lambda x, y: clicks.append([x, y]) or SimpleNamespace(
                    wait=lambda: SimpleNamespace(status=SimpleNamespace(succeeded=True))))
            context = SimpleNamespace(tasker=SimpleNamespace(controller=controller),
                                      run_recognition=lambda *a, **k: None if health is None else
                                      SimpleNamespace(hit=True, best_result=SimpleNamespace(text=f"{health}/34")))
            action = ProduceChooseWorkAuto()
            with patch.object(action, "_get_health_position", return_value=[2]), patch("produce.options.SESSIONS.get", return_value=None), patch("produce.options.log_operation"):
                self.assertTrue(action.run(context, None))
            self.assertEqual(clicks, [expected])
            self.assertEqual(action.health_observation["status"], "unknown" if health is None else "observed")

    def test_cache_conflict_does_not_choose_closest_identity(self):
        cache = self.identity_cache(self.directory / "cache", {"skill_cards": "s"})
        event = {"event_id": "synthetic:1", "candidate": {"box": [55, 790, 150, 150]}}
        for identifier in ("first", "second"):
            cache.learn(self.image, event, {"detail_verified": True, "identity": {
                "status": "matched", "needs_full_ocr": False, "effective_effect_dsl": "effect",
                "catalog_id": identifier, "database_name": identifier, "item_kind": "skill_card"}})
        self.assertIsNone(cache.lookup(self.image, event["candidate"]["box"]))

    def test_session_resume_keeps_display_directory_and_id(self):
        from produce.sessions import ProduceSessions
        manager = ProduceSessions()
        self.addCleanup(lambda: [item["capture"].close() for item in manager.active.values()])
        nodes = {
            "Produce": {"attach": {"save_screenshots": False}},
            "ProduceChooseIdol": {"recognition": {"param": {"custom_recognition_param": {"idol_name": "偶像/测试", "song_name": "歌曲:测试"}}}},
            "ProduceChooseDifficulty": {"recognition": {"param": {"expected": ["PRO"]}}},
            "ProduceEntryFlag": {"next": ["ProduceEntryNIA"]},
        }
        context = SimpleNamespace(tasker=SimpleNamespace(controller=SimpleNamespace(uuid="synthetic-controller")),
                                  get_task_job=lambda: SimpleNamespace(job_id=1), get_node_data=lambda name: nodes.get(name, {}))
        with patch("produce.sessions.ROOT", self.directory), patch.object(DecisionCapture, "start_analysis"), patch("produce.reporting.schedule_report"):
            first = manager.ensure(context, "ProduceEntryNIA", fresh_start=True)
            self.assertIsNotNone(first)
            identifier = first["state"]["session_id"]
            directory = first["path"].parent.parent
            self.assertIn("偶像_测试_歌曲_测试", directory.name)
            self.assertRegex(directory.name, r"^\d{8}_\d{4}_偶像_测试_歌曲_测试$")
            manager.close(manager.key(context.tasker), "interrupted")
            resumed = manager.ensure(context, "ProduceSkipPreparation")
            self.assertEqual(resumed["state"]["session_id"], identifier)
            self.assertEqual(resumed["path"].parent.parent, directory)
            self.assertEqual(len(resumed["state"]["segments"]), 2)
            manager.close(manager.key(context.tasker), "returned_home", terminal=True)
            self.assertFalse(resumed["pointer"].exists())
            fresh = manager.ensure(context, "ProduceEntryNIA", fresh_start=True)
            self.assertNotEqual(fresh["state"]["session_id"], identifier)
            manager.close(manager.key(context.tasker), "returned_home", terminal=True)

    def test_low_space_disables_images_but_preserves_text(self):
        capture = self.capture()
        capture._min_free_bytes = 100
        with patch("produce.capture.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            capture.record("decision_before", image=self.image)
            capture.record("decision_after", image=self.image)
            capture.close()
        events = [json.loads(line) for line in (capture.directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["frame_missing_reason"] == "capture_low_disk_space" for event in events))


if __name__ == "__main__":
    unittest.main()
