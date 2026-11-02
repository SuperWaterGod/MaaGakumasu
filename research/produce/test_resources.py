"""培育资源更新回归；使用本地 HTTP 和合成文件，不连接游戏或云账户。"""

import io
import os
import csv
import sys
import json
import shutil
import hashlib
import tempfile
import unittest
import threading
import contextlib
import subprocess
import importlib.util
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "tools")]

from produce.resources import (
    STATE_NAME,
    PENDING_NAME,
    RecoveryError,
    ResourceUpdater,
    UpdateCancelled,
    file_hash,
    atomic_json,
    managed_path,
    validate_manifest,
    ensure_resources_ready,
)
from build_produce_resources import Publisher, build, read_table


class CsvTest(unittest.TestCase):
    def test_upstream_field_types_and_quoted_values(self):
        rows = read_table(b'id,name,enabled,count,empty,dsl\r\n1,"a, ""b""",TRUE,007,,score+=2\r\n2,"line\nname",FALSE,-2,,\r\n')
        self.assertEqual(rows, [
            {"id": 1, "name": 'a, "b"', "enabled": True, "count": 7, "empty": "", "dsl": "score+=2"},
            {"id": 2, "name": "line\nname", "enabled": False, "count": "-2", "empty": "", "dsl": ""},
        ])


class ResourceTest(unittest.TestCase):
    def setUp(self):
        parent = ROOT.parent / "MaaGakumasuLab/debug/research/produce/tests"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="resources_", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.root = self.directory / "client"
        (self.root / "data").mkdir(parents=True)
        self.served = {}
        self.requests = []
        served, requests = self.served, self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Range")))
                if self.path not in served:
                    self.send_error(404)
                    return
                content = served[self.path]
                offset = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-")) if self.headers.get("Range") else 0
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(content) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(content) - 1}/{len(content)}")
                self.end_headers()
                self.wfile.write(content[offset:])

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.updater = ResourceUpdater(self.root, f"http://127.0.0.1:{server.server_port}")
        self.source = self.directory / "upstream"
        tables = self.source / "packages/gakumas-data/csv"
        tables.mkdir(parents=True)
        (self.source / "LICENSE").write_text("合成测试许可证", encoding="utf-8")
        card = {"id": 1, "name": "测试卡", "cost": "cost-=1", "conditions": "", "actions": "score+=1", "effects": ""}
        for name, rows in (("skill_cards", [card]), ("p_drinks", [{"id": 1, "name": "测试饮料", "actions": "genki+=1"}]),
                           ("p_items", [{"id": 1, "name": "测试道具", "effects": ""}])):
            self.write_table(name, rows)
        for folder in ("skillCards", "pDrinks", "pItems"):
            self.add_image(folder, "1.png")
        self.counter = 0

    def write_table(self, name, rows):
        path = self.source / "packages/gakumas-data/csv" / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({key: "TRUE" if value is True else "FALSE" if value is False else value
                             for key, value in row.items()} for row in rows)

    def add_image(self, folder, name, color=40):
        directory = self.source / "packages/gakumas-images/images" / folder / "icons"
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (128, 128), (color, 80, 140)).save(directory / name)

    def publish(self):
        self.counter += 1
        output = self.directory / f"build{self.counter}"
        with contextlib.redirect_stdout(io.StringIO()), patch(
            "build_produce_resources.subprocess.check_output", side_effect=["a" * 40, "2026-01-01T00:00:00+00:00"]
        ):
            manifest = build(self.source, output)
        version = manifest["version"]
        self.served["/v1/latest.json"] = (output / "latest.json").read_bytes()
        self.served[f"/v1/releases/{version}/manifest.json"] = (output / "manifest.json").read_bytes()
        self.served[f"/v1/releases/{version}/full.zip"] = (output / "full.zip").read_bytes()
        for row in manifest["files"]:
            self.served[f"/v1/objects/{row['sha256']}"] = (output / "payload" / row["path"]).read_bytes()
        return manifest, output

    def assert_ready(self):
        ensure_resources_ready(self.root)
        self.assertFalse((self.root / "data" / PENDING_NAME).exists())

    def test_full_install_and_temp_cleanup(self):
        manifest, output = self.publish()
        foreign = self.root / "temp/temp_res/mfa.txt"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("MFA")
        result = self.updater.update()
        self.assertEqual(result["status"], "updated")
        self.assertTrue(any(path.endswith("/full.zip") for path, _ in self.requests))
        self.assertTrue((self.root / "data/images/skillCards/1.png").is_file())
        self.assertFalse((self.root / "data/images/skillCards/icons").exists())
        self.assertFalse((self.updater.temp / manifest["version"]).exists())
        self.assertEqual(foreign.read_text(), "MFA")
        self.assert_ready()
        self.requests.clear()
        self.assertEqual(self.updater.update()["status"], "unchanged")
        self.assertEqual(self.requests, [("/v1/latest.json", None)])

    def test_incremental_logs_are_aggregated_and_details_are_debug_only(self):
        for number in range(1, 33):
            self.add_image("skillCards", f"1_{number}.png")
        self.publish()
        self.updater.update()
        names = [f"images/skillCards/1_{number}.png" for number in range(1, 9)]
        for name in names:
            (self.root / "data" / name).write_bytes(b"damaged")
        info, debug = [], []
        self.updater.log, self.updater.debug = info.append, debug.append
        self.requests.clear()
        result = self.updater.update()
        self.assertEqual(result["updated"], len(names))
        self.assertEqual(len(debug), len(names))
        self.assertFalse(any("images/" in message for message in info))
        self.assertTrue(any("增量更新进度：8/8" in message for message in info))
        self.assertEqual(len([path for path, _ in self.requests if "/objects/" in path]), len(names))

    def test_many_small_changes_use_one_bundle(self):
        import zipfile

        from produce.resources import MAX_INCREMENTAL_FILES

        names = [f"images/skillCards/1_{number}.png" for number in range(1, MAX_INCREMENTAL_FILES + 1)]
        for name in names:
            self.add_image("skillCards", Path(name).name)
        manifest, output = self.publish()
        # 有效 ZIP 注释扩大整包，确保此次切换确实由请求数量触发。
        with zipfile.ZipFile(output / "full.zip", "a") as archive:
            archive.comment = b"synthetic" * 7000
        manifest["bundle"] = {"size": (output / "full.zip").stat().st_size, "sha256": file_hash(output / "full.zip")}
        atomic_json(output / "manifest.json", manifest)
        latest = json.loads((output / "latest.json").read_text())
        latest.update(manifest_size=(output / "manifest.json").stat().st_size, manifest_sha256=file_hash(output / "manifest.json"))
        self.served["/v1/latest.json"] = json.dumps(latest).encode()
        prefix = f"/v1/releases/{manifest['version']}"
        self.served[prefix + "/manifest.json"] = (output / "manifest.json").read_bytes()
        self.served[prefix + "/full.zip"] = (output / "full.zip").read_bytes()
        self.updater.update()
        for name in names:
            (self.root / "data" / name).write_bytes(b"damaged")
        required = sum(row["size"] for row in manifest["files"] if row["path"] in names)
        self.assertLess(required, manifest["bundle"]["size"])
        self.requests.clear()
        self.assertEqual(self.updater.update()["updated"], MAX_INCREMENTAL_FILES)
        self.assertEqual(self.requests, [("/v1/latest.json", None), (prefix + "/full.zip", None)])

    def test_incremental_reuses_manual_icons_and_repairs(self):
        manifest, output = self.publish()
        shutil.copytree(output / "payload", self.root / "data", dirs_exist_ok=True)
        (self.root / "data/produce_skill_cards.json").write_text("broken")
        result = self.updater.update()
        self.assertEqual(result["updated"], 1)
        downloads = [path for path, _ in self.requests if "/objects/" in path]
        self.assertEqual(len(downloads), 1)
        self.assertFalse(any(path.endswith("/full.zip") for path, _ in self.requests))
        self.assert_ready()

    def test_changed_icon_and_database_are_installed_together(self):
        self.publish()
        self.updater.update()
        self.add_image("skillCards", "1.png", color=180)
        table = self.source / "packages/gakumas-data/csv/skill_cards.csv"
        rows = read_table(table.read_bytes())
        rows[0]["name"] = "新的测试卡"
        self.write_table("skill_cards", rows)
        manifest, output = self.publish()
        self.requests.clear()
        self.updater.update()
        self.assertEqual((self.root / "data/produce_skill_cards.json").read_bytes(), (output / "payload/produce_skill_cards.json").read_bytes())
        self.assertEqual(file_hash(self.root / "data/images/skillCards/1.png"), file_hash(output / "payload/images/skillCards/1.png"))
        self.assertFalse(any(path.endswith("/full.zip") for path, _ in self.requests))
        self.assert_ready()

    def test_removed_managed_files_preserve_unmanaged_files(self):
        self.add_image("skillCards", "1_1.png")
        self.publish()
        self.updater.update()
        manual = self.root / "data/images/skillCards/9999.png"
        manual.write_bytes(b"manual")
        (self.source / "packages/gakumas-images/images/skillCards/icons/1_1.png").unlink()
        self.publish()
        self.updater.update()
        self.assertFalse((self.root / "data/images/skillCards/1_1.png").exists())
        self.assertEqual(manual.read_bytes(), b"manual")

    def test_bad_download_never_overwrites_existing_files(self):
        manifest, output = self.publish()
        shutil.copytree(output / "payload", self.root / "data", dirs_exist_ok=True)
        first = manifest["files"][0]
        (self.root / "data" / first["path"]).write_bytes(b"original")
        before = (self.root / "data" / first["path"]).read_bytes()
        self.served[f"/v1/objects/{first['sha256']}"] = b"x" * first["size"]
        logs = []
        self.updater.log = logs.append
        with patch("produce.resources.time.sleep"):
            with self.assertRaises(ValueError):
                self.updater.update()
        self.assertEqual((self.root / "data" / first["path"]).read_bytes(), before)
        self.assertFalse((self.root / "data" / PENDING_NAME).exists())
        self.assertTrue(any(first["path"] in message for message in logs))

    def test_download_resume_and_cancellation(self):
        manifest, output = self.publish()
        content = (output / "full.zip").read_bytes()
        target = self.updater.temp / manifest["version"] / "full.zip.part"
        target.parent.mkdir(parents=True)
        target.write_bytes(content[:100])
        self.updater.download(f"/v1/releases/{manifest['version']}/full.zip", target, len(content), hashlib.sha256(content).hexdigest())
        self.assertIn((f"/v1/releases/{manifest['version']}/full.zip", "bytes=100-"), self.requests)
        self.assertEqual(target.read_bytes(), content)
        self.updater.stopping = lambda: True
        with self.assertRaises(UpdateCancelled):
            self.updater.update()

    def test_low_space_and_incompatible_manifest(self):
        self.publish()
        with patch("produce.resources.shutil.disk_usage", return_value=type("Usage", (), {"free": 1})()):
            with self.assertRaises(OSError):
                self.updater.update()
        self.assertFalse((self.root / "data/produce_skill_cards.json").exists())
        latest = json.loads(self.served["/v1/latest.json"])
        latest["protocol"] = 2
        self.served["/v1/latest.json"] = json.dumps(latest).encode()
        with self.assertRaises(ValueError):
            self.updater.update()

    def test_recover_partial_install_and_repeated_recovery(self):
        manifest, output = self.publish()
        shutil.copytree(output / "payload", self.root / "data", dirs_exist_ok=True)
        name = "images/skillCards/1.png"
        original = (self.root / "data" / name).read_bytes()
        work = self.updater.temp / manifest["version"]
        backup = work / "backup" / name
        backup.parent.mkdir(parents=True)
        backup.write_bytes(original)
        (self.root / "data" / name).write_bytes(b"partial")
        atomic_json(self.root / "data" / PENDING_NAME, {"manifest": manifest, "deleted": [], "operations": [
            {"path": name, "existed": True, "sha256": hashlib.sha256(original).hexdigest()}]})
        self.updater.recover()
        self.updater.recover()
        self.assertEqual((self.root / "data" / name).read_bytes(), original)
        self.assert_ready()

    def test_mfa_cleaned_temp_requires_repair(self):
        manifest, output = self.publish()
        shutil.copytree(output / "payload", self.root / "data", dirs_exist_ok=True)
        name = "images/skillCards/1.png"
        original = (self.root / "data" / name).read_bytes()
        (self.root / "data" / name).write_bytes(b"partial")
        atomic_json(self.root / "data" / PENDING_NAME, {"manifest": manifest, "deleted": [], "operations": [
            {"path": name, "existed": True, "sha256": hashlib.sha256(original).hexdigest()}]})
        with self.assertRaises(RecoveryError):
            ensure_resources_ready(self.root)
        self.updater.update()
        self.assertEqual((self.root / "data" / name).read_bytes(), original)
        self.assert_ready()

    def test_install_failure_rolls_back(self):
        self.publish()
        self.updater.update()
        self.add_image("skillCards", "1.png", color=210)
        manifest, output = self.publish()
        before = {path.relative_to(self.root / "data"): path.read_bytes() for path in (self.root / "data").rglob("*") if path.is_file()}
        replace = os.replace
        hit = False

        def fail_once(source, target):
            nonlocal hit
            if "stage" in Path(source).parts and not hit:
                hit = True
                raise PermissionError("模拟文件被占用")
            return replace(source, target)

        with patch("produce.resources.os.replace", side_effect=fail_once):
            with self.assertRaises(PermissionError):
                self.updater.update()
        self.assertTrue(hit)
        after = {path.relative_to(self.root / "data"): path.read_bytes() for path in (self.root / "data").rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assert_ready()

    def test_zip_and_manifest_paths_cannot_escape(self):
        manifest, output = self.publish()
        import zipfile

        archive = self.directory / "bad.zip"
        for name in ("../escape.png", "images/skillCards/icons/1.png", "agent/main.py", "images/skillCards/1.png/extra"):
            with self.assertRaises(ValueError):
                managed_path(self.root / "data", name)
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr(name, b"bad")
            with self.assertRaises(ValueError):
                self.updater.unpack(archive, self.directory / "stage", manifest)
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("images/skillCards/1.png", b"bad")
        with self.assertRaises(ValueError):
            self.updater.unpack(archive, self.directory / "stage", manifest)

    def test_builder_repeatable_and_unchanged_skips_publish(self):
        first, output = self.publish()
        second, other = self.publish()
        self.assertEqual(first["version"], second["version"])
        self.assertEqual((output / "full.zip").read_bytes(), (other / "full.zip").read_bytes())
        self.assertEqual((output / "manifest.json").read_bytes(), (other / "manifest.json").read_bytes())
        self.assertIsNone(build(self.source, self.directory / "unused", previous=first))
        invalid = json.loads(json.dumps(first))
        invalid["files"][0]["path"] = "../escape"
        with self.assertRaises(ValueError):
            validate_manifest(invalid)

    def test_source_and_installed_layout_resolution(self):
        from produce.resources import data_directory

        root = self.directory / "checkout"
        (root / "assets/data").mkdir(parents=True)
        self.assertEqual(data_directory(root), root / "assets/data")
        self.assertEqual(data_directory(self.root), self.root / "data")

    def test_corrupted_local_manifest_can_be_repaired(self):
        self.publish()
        self.updater.update()
        (self.root / "data" / STATE_NAME).write_text("broken")
        self.requests.clear()
        self.assertEqual(self.updater.update()["status"], "unchanged")
        self.assertTrue(any(path.endswith("/manifest.json") for path, _ in self.requests))
        self.assert_ready()

    def test_parallel_updaters_do_not_share_a_transaction(self):
        self.publish()
        second = ResourceUpdater(self.root, self.updater.base_url)
        with self.updater.locked():
            with self.assertRaises(OSError):
                second.update()

    @unittest.skipUnless(importlib.util.find_spec("maa"), "动作入口回归需要可选的 MaaFramework 开发环境")
    def test_cache_is_refreshed_without_restarting_agent(self):
        # 框架动作注册会切换 Maa 的动态库模式，独立进程避免污染后续离线识别回归。
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "agent"))
from types import SimpleNamespace
from unittest.mock import patch
from custom.action.produce import ProduceUpdateResources
from produce.icons import load_gallery
context = SimpleNamespace(tasker=SimpleNamespace(stopping=False))
with patch("custom.action.produce.SESSIONS") as sessions, patch("custom.action.produce.configured_updater") as configured:
    sessions.active = {}
    configured.return_value.update.return_value = {"status": "updated"}
    with patch.object(load_gallery, "cache_clear") as clear:
        assert ProduceUpdateResources().run(context, None)
        clear.assert_called_once()
    sessions.active = {"active": object()}
    configured.reset_mock()
    assert ProduceUpdateResources().run(context, None)
    configured.assert_not_called()
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, encoding="utf-8",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)


class PublisherTest(unittest.TestCase):
    def test_rollback_checks_objects_before_changing_pointer(self):
        from produce.resources import DATABASES

        files = [{"path": name, "size": 10, "sha256": "b" * 64} for name in sorted(DATABASES)]
        version = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest = {"protocol": 1, "version": version, "files": files, "bundle": {"size": 50, "sha256": "c" * 64}}
        content = json.dumps(manifest).encode()
        calls = []

        class Client:
            missing = False

            def get_object(self, **kwargs):
                return {"Body": io.BytesIO(content)}

            def head_object(self, **kwargs):
                if self.missing:
                    raise OSError("缺少回退资源")
                calls.append(kwargs["Key"])

        publisher = Publisher.__new__(Publisher)
        publisher.bucket = "test"
        publisher.client = Client()
        publisher.get_json = lambda key: manifest
        publisher.verified_upload = lambda key, path, **kwargs: calls.append(key)
        parent = ROOT.parent / "MaaGakumasuLab/debug/research/produce/tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rollback_", dir=parent) as directory:
            publisher.rollback(version, directory)
            self.assertEqual(calls[-1], "v1/latest.json")
            latest = json.loads((Path(directory) / "latest.json").read_text())
            self.assertEqual(latest["manifest_sha256"], hashlib.sha256(content).hexdigest())
            calls.clear()
            publisher.client.missing = True
            with self.assertRaises(OSError):
                publisher.rollback(version, directory)
            self.assertNotIn("v1/latest.json", calls)

    def test_pointer_is_last(self):
        publisher = Publisher.__new__(Publisher)
        publisher.bucket = "test"
        uploads = []
        publisher.verified_upload = lambda key, path, **kwargs: uploads.append(key)
        publisher.prune = lambda version: uploads.append("prune")
        publisher.publish(Path("build"), {"version": "a" * 64, "files": [
            {"path": "image.png", "sha256": "b" * 64}, {"path": "same.png", "sha256": "b" * 64},
        ]})
        self.assertEqual(uploads[-2:], ["v1/latest.json", "prune"])
        self.assertEqual(uploads[0], "v1/objects/" + "b" * 64)
        self.assertEqual(uploads.count("v1/objects/" + "b" * 64), 1)

    def test_object_upload_failure_does_not_publish_pointer(self):
        publisher = Publisher.__new__(Publisher)
        publisher.bucket = "test"
        uploaded = []

        def upload(key, path, **kwargs):
            if key.endswith("b" * 64):
                raise OSError("模拟 R2 上传失败")
            uploaded.append(key)

        publisher.verified_upload = upload
        publisher.prune = lambda version: uploaded.append("prune")
        with self.assertRaises(OSError):
            publisher.publish(Path("build"), {"version": "a" * 64, "files": [
                {"path": "1.png", "sha256": "c" * 64}, {"path": "2.png", "sha256": "b" * 64},
            ]})
        self.assertNotIn("v1/latest.json", uploaded)
        self.assertNotIn("prune", uploaded)

    def test_pruning_keeps_fourteen_versions_and_shared_objects(self):
        from produce.resources import DATABASES

        manifests = {}
        objects = []
        for number in range(16):
            unique = hashlib.sha256(str(number).encode()).hexdigest()
            files = [{"path": name, "size": 10, "sha256": unique if name == "produce_resources_source.json" else "b" * 64}
                     for name in sorted(DATABASES)]
            version = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            manifest = {"protocol": 1, "version": version, "files": files, "bundle": {"size": 50, "sha256": "c" * 64}}
            key = f"v1/releases/{version}/manifest.json"
            manifests[key] = manifest
            for suffix in ("manifest.json", "full.zip"):
                objects.append({"Key": f"v1/releases/{version}/{suffix}", "LastModified": number})
            objects.append({"Key": f"v1/objects/{unique}", "LastModified": number})
        objects.append({"Key": "v1/objects/" + "b" * 64, "LastModified": 0})
        objects.append({"Key": "v1/latest.json", "LastModified": 17})
        deleted = []

        class Client:
            def get_paginator(self, name):
                return self

            def paginate(self, **kwargs):
                yield {"Contents": objects[:20]}
                yield {"Contents": objects[20:]}

            def delete_objects(self, **kwargs):
                deleted.extend(row["Key"] for row in kwargs["Delete"]["Objects"])

        publisher = Publisher.__new__(Publisher)
        publisher.bucket = "test"
        publisher.client = Client()
        publisher.get_json = manifests.get
        current = next(reversed(manifests.values()))["version"]
        publisher.prune(current)
        self.assertEqual(len([key for key in deleted if key.endswith("/manifest.json")]), 2)
        self.assertEqual(len([key for key in deleted if "/objects/" in key]), 2)
        self.assertNotIn("v1/objects/" + "b" * 64, deleted)
        self.assertNotIn("v1/latest.json", deleted)
        self.assertFalse(any(current in key for key in deleted))


if __name__ == "__main__":
    unittest.main()
