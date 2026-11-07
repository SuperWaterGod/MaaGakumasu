"""培育数据库及原始图库更新；下载暂存于 temp，正式文件保持原有布局。"""

import os
import re
import json
import time
import shutil
import hashlib
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from functools import lru_cache
from contextlib import contextmanager

PROTOCOL = 1
DEFAULT_URL = "https://mag.2j.hk"
STATE_NAME = "produce_resources_manifest.json"
PENDING_NAME = "produce_resources_pending.json"
DATABASES = {"produce_skill_cards.json", "produce_reward_items.json", "GAKUMAS_DATA_LICENSE.txt", "produce_resources_source.json"}
ICON_FOLDERS = {"skill_card": "skillCards", "drink": "pDrinks", "p_item": "pItems"}
PNG_PATH = re.compile(r"images/(skillCards|pDrinks|pItems)/[1-9][0-9]*(?:_[1-9][0-9]*)?\.png\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_MANIFEST = 8 * 1024**2
MAX_TOTAL = 1024**3
MAX_INCREMENTAL_FILES = 256


class UpdateCancelled(Exception):
    """用户停止更新；保留可续传的下载。"""


class RecoveryError(RuntimeError):
    """无法确认正式资源完整性，禁止开始新的培育。"""


def data_directory(root):
    root = Path(root).resolve()
    return root / "data" if (root / "data").is_dir() else root / "assets/data"


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def managed_path(directory, name):
    if not isinstance(name, str) or (name not in DATABASES and not PNG_PATH.fullmatch(name)):
        raise ValueError(f"不允许更新的文件路径: {name}")
    root = Path(directory).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"文件路径超出数据目录: {name}")
    return path


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL or not SHA256.fullmatch(str(value.get("version", ""))):
        raise ValueError("不兼容的资源清单")
    files = value.get("files")
    if not isinstance(files, list) or not files or len(files) > 30000:
        raise ValueError("无效的资源文件列表")
    names = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("无效的资源条目")
        name = entry.get("path")
        managed_path(Path.cwd(), name)
        if name in names or not SHA256.fullmatch(str(entry.get("sha256", ""))):
            raise ValueError("重复路径或无效文件哈希")
        size = entry.get("size")
        if type(size) is not int or not 0 < size <= 32 * 1024**2:
            raise ValueError("无效的资源大小")
        total += size
        names.add(name)
    if not DATABASES.issubset(names) or total > MAX_TOTAL:
        raise ValueError("资源缺少数据库或超过容量限制")
    bundle = value.get("bundle", {})
    if (not isinstance(bundle, dict) or not SHA256.fullmatch(str(bundle.get("sha256", "")))
            or type(bundle.get("size")) is not int or not 0 < bundle["size"] <= MAX_TOTAL):
        raise ValueError("无效的整包信息")
    # 资源版本由文件内容确定，下载时间不参与版本计算。
    canonical = json.dumps(sorted(files, key=lambda row: row["path"]), sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != value["version"]:
        raise ValueError("资源版本与文件清单不一致")
    return value


def files_valid(directory, manifest):
    return all((path := managed_path(directory, entry["path"])).is_file() and path.stat().st_size == entry["size"]
               and file_hash(path) == entry["sha256"] for entry in manifest["files"])


@lru_cache(maxsize=2)
def icon_resources_status(root):
    """按已安装清单核验各类图库；入口刷新缓存，单轮内复用结果。"""
    directory = data_directory(root)
    manifest = validate_manifest(json.loads((directory / STATE_NAME).read_text(encoding="utf-8")))
    status = {kind: {"expected": 0, "missing": 0, "damaged": 0, "complete": False} for kind in ICON_FOLDERS}
    kinds = {folder: kind for kind, folder in ICON_FOLDERS.items()}
    for entry in manifest["files"]:
        if not PNG_PATH.fullmatch(entry["path"]):
            continue
        row = status[kinds[entry["path"].split("/")[1]]]
        row["expected"] += 1
        path = managed_path(directory, entry["path"])
        try:
            if not path.is_file():
                row["missing"] += 1
            elif path.stat().st_size != entry["size"] or file_hash(path) != entry["sha256"]:
                row["damaged"] += 1
        except OSError:
            row["damaged"] += 1
    for row in status.values():
        row["complete"] = row["expected"] > 0 and row["missing"] == 0 and row["damaged"] == 0
    return status


def validate_catalogs(directory):
    """核验运行时需要的最小数据结构，不要求上游为每个条目提供图标。"""
    directory = Path(directory)
    skills = json.loads((directory / "produce_skill_cards.json").read_text(encoding="utf-8"))
    items = json.loads((directory / "produce_reward_items.json").read_text(encoding="utf-8"))
    if skills.get("schema_version") != 1 or items.get("schema_version") != 1:
        raise ValueError("不兼容的数据库格式")
    revision = skills.get("source", {}).get("revision", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision != items.get("source", {}).get("revision"):
        raise ValueError("数据库来源版本不一致")
    tables = [(skills.get("cards"), ("cost", "actions", "effects", "conditions"), int),
              (items.get("drinks"), ("actions",), int), (items.get("p_items"), ("effects",), int)]
    if "support_cards" in items:
        tables.append((items["support_cards"], (), str))
    for rows, fields, id_type in tables:
        if not isinstance(rows, list) or not rows:
            raise ValueError("数据库缺少条目")
        ids = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get("id")) is not id_type or row["id"] in ids
                    or (id_type is int and row["id"] <= 0)
                    or (id_type is str and not re.fullmatch(r"s_card-[0-9]+-[0-9]+", row["id"]))
                    or not isinstance(row.get("name"), str) or not row["name"]
                    or not isinstance(row.get("aliases", []), list)
                    or any(not isinstance(name, str) or not name.strip() for name in row.get("aliases", []))
                    or ("name_zh_cn" in row and (not isinstance(row["name_zh_cn"], str) or not row["name_zh_cn"].strip()))
                    or any(not isinstance(row.get(field, ""), str) for field in fields)):
                raise ValueError("数据库条目格式错误或 ID 重复")
            ids.add(row["id"])


class ResourceUpdater:
    """同步检查、下载和事务安装；网络失败不得破坏原有资源。"""

    def __init__(self, root, base_url=DEFAULT_URL, *, stopping=None, log=None, debug=None, timeout=20):
        self.root = Path(root).resolve()
        self.data = data_directory(self.root)
        self.temp = self.root / "temp/produce_update"
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("更新地址必须使用 HTTPS")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("无效的更新地址")
        self.stopping = stopping or (lambda: False)
        self.log = log or (lambda message: None)
        self.debug = debug or (lambda message: None)
        self.timeout = timeout
        self.work = None
        self.repairing = False
        self._lock_depth = 0

    @contextmanager
    def locked(self):
        """用系统文件锁避免多个 Agent 同时修改同一安装目录；进程退出自动释放。"""
        if self._lock_depth:
            yield
            return
        lock = self.root / "config/produce_resources.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def check_stop(self):
        if self.stopping():
            raise UpdateCancelled("用户已停止更新")

    def read_json(self, suffix):
        self.check_stop()
        request = urllib.request.Request(self.base_url + suffix, headers={"Cache-Control": "no-cache", "User-Agent": "MaaGakumasu-resources/1"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise ValueError("更新清单过大")
        return json.loads(raw)

    def download(self, suffix, target, size, digest, *, progress=True):
        """按不可变 URL 续传；服务端忽略 Range 时从头下载。"""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and target.stat().st_size == size and file_hash(target) == digest:
            return
        for attempt in range(3):
            self.check_stop()
            offset = target.stat().st_size if target.exists() else 0
            if offset >= size:
                target.unlink()
                offset = 0
            headers = {"User-Agent": "MaaGakumasu-resources/1"}
            if offset:
                headers.update({"Range": f"bytes={offset}-", "If-Range": f'"{digest}"'})
            request = urllib.request.Request(self.base_url + suffix, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    resumed = response.status == 206
                    if resumed and response.headers.get("Content-Range") != f"bytes {offset}-{size - 1}/{size}":
                        raise ValueError("续传响应范围不符")
                    written = offset if resumed else 0
                    last_notice = time.monotonic()
                    with target.open("ab" if resumed else "wb") as stream:
                        while True:
                            self.check_stop()
                            block = response.read(256 * 1024)
                            if not block:
                                break
                            written += len(block)
                            if written > size:
                                raise ValueError("下载内容超过声明大小")
                            stream.write(block)
                            if progress and size >= 1024**2 and (time.monotonic() - last_notice >= 2 or written == size):
                                self.log(f"下载进度：{written / 1024**2:.1f}/{size / 1024**2:.1f} MiB（{written * 100 / size:.0f}%）")
                                last_notice = time.monotonic()
                if target.stat().st_size != size:
                    raise OSError("下载未完成")
                if file_hash(target) != digest:
                    target.unlink()
                    raise ValueError("下载文件 SHA256 不符")
                return
            except (OSError, ValueError) as exc:
                if attempt == 2:
                    raise
                self.log(f"下载重试 {attempt + 1}/2（{target.name}）：{exc}")
                for _ in range(5):
                    self.check_stop()
                    time.sleep(.1)

    def clean_work(self, work):
        work = Path(work)
        if work.parent.resolve() != self.temp.resolve() or not SHA256.fullmatch(work.name) or work.is_symlink():
            raise RecoveryError("临时目录路径不合法，拒绝清理")
        if work.exists():
            shutil.rmtree(work)

    def finish_work(self, work):
        try:
            self.clean_work(work)
        except OSError as exc:
            # 安装已经提交，清理失败不能阻止调用方刷新内存图库。
            self.log(f"正式资源已就绪，但临时文件清理失败，下次检查时重试：{exc}")

    def recover(self):
        """持久标记保存在 data，MFA 清理 temp 后也不会静默忽略中断。"""
        with self.locked():
            self._recover()

    def _recover(self):
        pending = self.data / PENDING_NAME
        if not pending.exists():
            self.install_bundled_database()
            return
        try:
            journal = json.loads(pending.read_text(encoding="utf-8"))
            manifest = validate_manifest(journal["manifest"])
            work = self.temp / manifest["version"]
            if files_valid(self.data, manifest) and all(not managed_path(self.data, name).exists() for name in journal["deleted"]):
                atomic_json(self.data / STATE_NAME, manifest)
            else:
                if journal.get("repairing"):
                    raise RecoveryError("资源修复尚未完成，请重新执行资源更新")
                operations = journal["operations"]
                # 先检查全部恢复材料，不能恢复一半才发现备份已被 MFA 清除。
                for entry in operations:
                    managed_path(self.data, entry["path"])
                    if entry["existed"]:
                        backup = managed_path(work / "backup", entry["path"])
                        if not backup.is_file() or file_hash(backup) != entry["sha256"]:
                            raise RecoveryError("更新中断且备份缺失或损坏，请重新执行资源更新修复")
                for entry in operations:
                    destination = managed_path(self.data, entry["path"])
                    if entry["existed"]:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        restore = destination.with_name(destination.name + ".restore")
                        shutil.copyfile(managed_path(work / "backup", entry["path"]), restore)
                        os.replace(restore, destination)
                    else:
                        destination.unlink(missing_ok=True)
            pending.unlink()
            self.finish_work(work)
            self.log("已处理上次中断的资源安装")
        except RecoveryError:
            raise
        except Exception as exc:
            raise RecoveryError(f"资源恢复失败：{exc}") from exc

    def install_bundled_database(self):
        """首次运行时事务安装内置数据库；已安装的资源由更新任务维护。"""
        bundled = self.data / "bundled"
        manifest_file = bundled / STATE_NAME
        state = self.data / STATE_NAME
        if state.exists():
            if bundled.is_dir():
                try:
                    installed = validate_manifest(json.loads(state.read_text(encoding="utf-8")))
                    core = {**installed, "files": [entry for entry in installed["files"] if entry["path"] in DATABASES]}
                    if files_valid(self.data, core):
                        validate_catalogs(self.data)
                        self.discard_bundled_database()
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            return
        if not manifest_file.is_file():
            return
        try:
            manifest = validate_manifest(json.loads(manifest_file.read_text(encoding="utf-8")))
            if {entry["path"] for entry in manifest["files"]} != DATABASES or not files_valid(bundled, manifest):
                raise ValueError("内置数据库文件与清单不一致")
            validate_catalogs(bundled)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryError(f"内置数据库校验失败，请执行“更新数据库和素材”：{exc}") from exc
        self.work = self.temp / manifest["version"]
        stage = self.work / "stage"
        stage.mkdir(parents=True, exist_ok=True)
        for name in sorted(DATABASES):
            shutil.copyfile(bundled / name, stage / name)
        self.install(manifest, sorted(DATABASES), [])
        self.finish_work(self.work)
        self.log("已安装内置数据库，可通过“更新数据库和素材”获取更新及图标")

    def discard_bundled_database(self):
        """正式数据确认可用后清理内置副本，仅删除打包白名单内的文件。"""
        bundled = self.data / "bundled"
        if not bundled.is_dir() or bundled.is_symlink() or bundled.resolve().parent != self.data.resolve():
            return
        try:
            for name in sorted(DATABASES | {STATE_NAME}):
                (bundled / name).unlink(missing_ok=True)
            if not any(bundled.iterdir()):
                bundled.rmdir()
        except OSError as exc:
            self.log(f"数据库已就绪，内置副本将在下次启动时重试清理：{exc}")

    def install(self, manifest, changed, deleted):
        stage = self.work / "stage"
        operations = []
        for name in ([] if self.repairing else [*changed, *deleted]):
            self.check_stop()
            path = managed_path(self.data, name)
            entry = {"path": name, "existed": path.is_file()}
            if path.exists() and not path.is_file():
                raise ValueError(f"目标不是普通文件: {name}")
            if entry["existed"]:
                backup = managed_path(self.work / "backup", name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, backup)
                entry["sha256"] = file_hash(backup)
            operations.append(entry)
        # 在首次覆盖前写入完整日志；恢复过程可重复执行。
        atomic_json(self.data / PENDING_NAME, {"manifest": manifest, "operations": operations, "deleted": deleted, "repairing": self.repairing})
        try:
            for name in changed:
                self.check_stop()
                destination = managed_path(self.data, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(managed_path(stage, name), destination)
            for name in deleted:
                self.check_stop()
                managed_path(self.data, name).unlink(missing_ok=True)
            if not files_valid(self.data, manifest):
                raise ValueError("安装后资源校验失败")
            validate_catalogs(self.data)
            atomic_json(self.data / STATE_NAME, manifest)
            (self.data / PENDING_NAME).unlink()
        except BaseException:
            self.recover()
            raise
        self.discard_bundled_database()

    def unpack(self, bundle, stage, manifest):
        expected = {entry["path"]: entry for entry in manifest["files"]}
        seen = set()
        with zipfile.ZipFile(bundle) as archive:
            for info in archive.infolist():
                self.check_stop()
                name = info.filename
                path = managed_path(stage, name)
                if name not in expected or name in seen or info.file_size != expected[name]["size"] or info.is_dir():
                    raise ValueError("ZIP 包含意外文件或不符的大小")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("ZIP 不允许符号链接")
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, path.open("wb") as destination:
                    shutil.copyfileobj(source, destination, length=256 * 1024)
                seen.add(name)
        if seen != set(expected) or not files_valid(stage, manifest):
            raise ValueError("ZIP 资源缺失或哈希错误")

    def update(self):
        with self.locked():
            return self._update()

    def _update(self):
        try:
            self.recover()
        except RecoveryError:
            self.repairing = True
            self.log("上次安装无法恢复，将按远程完整清单修复；完成前不允许开始培育")
        latest = self.read_json("/v1/latest.json")
        version = latest.get("version", "")
        digest = latest.get("manifest_sha256", "")
        manifest_size = latest.get("manifest_size")
        if (latest.get("protocol") != PROTOCOL or not isinstance(version, str) or not SHA256.fullmatch(version)
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)
                or type(manifest_size) is not int or not 0 < manifest_size <= MAX_MANIFEST):
            raise ValueError("不兼容的更新入口")
        self.work = self.temp / version
        self.work.mkdir(parents=True, exist_ok=True)
        manifest_file = self.work / "manifest.json"
        state_file = self.data / STATE_NAME
        if state_file.is_file() and state_file.stat().st_size == manifest_size and file_hash(state_file) == digest:
            manifest = validate_manifest(json.loads(state_file.read_text(encoding="utf-8")))
        else:
            self.download(f"/v1/releases/{version}/manifest.json", manifest_file, manifest_size, digest)
            manifest = validate_manifest(json.loads(manifest_file.read_text(encoding="utf-8")))
        if manifest["version"] != version:
            raise ValueError("下载清单版本不一致")
        previous = None
        if state_file.exists():
            try:
                previous = validate_manifest(json.loads(state_file.read_text(encoding="utf-8")))
            except (ValueError, TypeError, KeyError):
                self.log("本地版本清单损坏，按远程清单修复；不删除无法确认归属的文件")
        by_name = {entry["path"]: entry for entry in manifest["files"]}
        changed = [name for name, entry in by_name.items() if not (path := managed_path(self.data, name)).is_file()
                   or path.stat().st_size != entry["size"] or file_hash(path) != entry["sha256"]]
        deleted = [entry["path"] for entry in previous["files"] if entry["path"] not in by_name] if previous else []
        if not changed and not deleted:
            validate_catalogs(self.data)
            atomic_json(state_file, manifest)
            (self.data / PENDING_NAME).unlink(missing_ok=True)
            self.finish_work(self.work)
            self.log("数据库和素材已是最新版本")
            return {"status": "unchanged", "version": version, "updated": 0}
        required = sum(by_name[name]["size"] for name in changed)
        bundle = manifest["bundle"]
        # 手工放置的图标也参与复用；没有可复用图标或增量请求过多时使用整包。
        reusable_icons = any(PNG_PATH.fullmatch(name) and name not in changed for name in by_name)
        full = not reusable_icons or len(changed) >= MAX_INCREMENTAL_FILES or required >= bundle["size"]
        needed = (sum(row["size"] for row in by_name.values()) + bundle["size"] if full else required)
        needed += sum(managed_path(self.data, name).stat().st_size for name in [*changed, *deleted] if managed_path(self.data, name).is_file())
        if shutil.disk_usage(self.work).free < needed + 32 * 1024**2:
            raise OSError("磁盘空间不足，保留原有资源")
        stage = self.work / "stage"
        self.log(f"准备{'整包' if full else '增量'}更新：{len(changed)} 个文件，下载约 {(bundle['size'] if full else required) / 1024**2:.1f} MiB")
        if full:
            archive = self.work / "full.zip.part"
            self.download(f"/v1/releases/{version}/full.zip", archive, bundle["size"], bundle["sha256"])
            self.unpack(archive, stage, manifest)
        else:
            last_notice = time.monotonic()
            prepared = 0
            for index, name in enumerate(changed, 1):
                entry = by_name[name]
                self.debug(f"下载资源 {index}/{len(changed)}：{name}")
                try:
                    self.download(f"/v1/objects/{entry['sha256']}", managed_path(stage, name), entry["size"], entry["sha256"], progress=False)
                except (OSError, ValueError):
                    self.log(f"资源下载失败：{name}")
                    raise
                prepared += entry["size"]
                if time.monotonic() - last_notice >= 2 or index == len(changed):
                    self.log(f"增量更新进度：{index}/{len(changed)} 个文件，{prepared / 1024**2:.1f}/{required / 1024**2:.1f} MiB")
                    last_notice = time.monotonic()
        # 数据库有未变化文件时借用正式文件校验，但不把整库复制进正式版本目录。
        for name in DATABASES:
            path = stage / name
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.data / name, path)
        validate_catalogs(stage)
        self.install(manifest, changed, deleted)
        self.finish_work(self.work)
        self.log(f"数据库和素材更新完成：{len(changed)} 个文件")
        return {"status": "updated", "version": version, "updated": len(changed)}


def configured_updater(root, **kwargs):
    root = Path(root).resolve()
    config = root / "config/produce_resources.json"
    base_url = DEFAULT_URL
    if config.exists():
        base_url = json.loads(config.read_text(encoding="utf-8"))["base_url"]
    return ResourceUpdater(root, base_url, **kwargs)


def ensure_resources_ready(root):
    """启动和培育入口先恢复中断事务，避免跳过更新任务后读取混合版本。"""
    updater = ResourceUpdater(root)
    updater.recover()
    state = updater.data / STATE_NAME
    if not state.is_file():
        raise RecoveryError("尚未安装培育数据库和素材，请先执行“更新数据库和素材”")
    manifest = validate_manifest(json.loads(state.read_text(encoding="utf-8")))
    # 数据库必须有效；图库由培育入口另行核验并按缺失范围降级。
    core = {**manifest, "files": [row for row in manifest["files"] if row["path"] in DATABASES]}
    if not files_valid(updater.data, core):
        raise RecoveryError("培育数据库与上次安装清单不一致，请先执行资源更新")
