"""从固定上游检出生成培育资源，并发布到 R2；客户端不依赖此维护工具。"""

import io
import os
import csv
import sys
import json
import shutil
import hashlib
import zipfile
import argparse
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from sync_skill_cards import build_database as build_skills
from produce.resources import PNG_PATH, PROTOCOL, file_hash, atomic_json, validate_catalogs, validate_manifest
from sync_reward_items import build_database as build_items

REPOSITORY = "https://github.com/gakumas-tools/gakumas-tools"
TABLES = {"cards": "skill_cards.csv", "drinks": "p_drinks.csv", "p_items": "p_items.csv"}


def read_table(raw):
    """按上游 utils/csv.js 的规则转换布尔值和纯数字，其余字段保留字符串。"""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("CSV 表头缺失或重复")
    rows = []
    for record in reader:
        if None in record or any(value is None for value in record.values()):
            raise ValueError("CSV 行的字段数与表头不一致")
        rows.append({key: True if value == "TRUE" else False if value == "FALSE"
                     else int(value) if value.isascii() and value.isdecimal() else value for key, value in record.items()})
    return rows


def selected_sources(source):
    source = Path(source).resolve()
    tables = source / "packages/gakumas-data/csv"
    selected = {f"tables/{key}": tables / name for key, name in TABLES.items()}
    selected["LICENSE"] = source / "LICENSE"
    for path in selected.values():
        if not path.is_file():
            raise ValueError(f"上游源文件缺失: {path}")
    for folder in ("skillCards", "pDrinks", "pItems"):
        directory = source / "packages/gakumas-images/images" / folder / "icons"
        if not directory.is_dir():
            raise ValueError(f"上游目录缺失: {directory}")
        paths = sorted(directory.glob("*.png"))
        if not paths:
            raise ValueError(f"上游图库为空: {directory}")
        for path in paths:
            name = f"images/{folder}/{path.name}"
            if not PNG_PATH.fullmatch(name) or path.is_symlink():
                raise ValueError(f"无效的上游图片: {path}")
            selected[name] = path
    return selected


def source_fingerprint(selected):
    rows = [{"path": name, "sha256": file_hash(path)} for name, path in sorted(selected.items())]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(source, output, previous=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    selected = selected_sources(source)
    fingerprint = source_fingerprint(selected)
    if previous and previous.get("source_fingerprint") == fingerprint:
        print("上游所选数据和原图没有变化，跳过发布")
        return None
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    timestamp = subprocess.check_output(["git", "-C", str(source), "show", "-s", "--format=%cI", "HEAD"], text=True).strip()
    raw = {key: selected[f"tables/{key}"].read_bytes() for key in TABLES}
    skills = build_skills(read_table(raw["cards"]), revision, hashlib.sha256(raw["cards"]).hexdigest())
    items = build_items({key: (read_table(raw[key]), hashlib.sha256(raw[key]).hexdigest()) for key in ("drinks", "p_items")}, revision)
    skills["source"]["path"] = "packages/gakumas-data/csv/" + TABLES["cards"]
    for key in ("drinks", "p_items"):
        items["source"]["tables"][key]["path"] = "packages/gakumas-data/csv/" + TABLES[key]
    # 固定为提交时间，使同一来源可以重复生成相同内容。
    skills["source"]["fetched_at"] = items["source"]["fetched_at"] = timestamp
    payload = output / "payload"
    payload.mkdir(parents=True, exist_ok=False)
    atomic_json(payload / "produce_skill_cards.json", skills)
    atomic_json(payload / "produce_reward_items.json", items)
    shutil.copyfile(selected["LICENSE"], payload / "GAKUMAS_DATA_LICENSE.txt")
    rows_by_folder = {"skillCards": skills["cards"], "pDrinks": items["drinks"], "pItems": items["p_items"]}
    coverage = {}
    for folder, rows in rows_by_folder.items():
        ids = {row["id"] for row in rows}
        covered = set()
        count = 0
        for name, path in selected.items():
            if not name.startswith(f"images/{folder}/"):
                continue
            identifier = int(path.stem.split("_")[0])
            if identifier not in ids:
                continue
            with Image.open(path) as image:
                if image.format != "PNG" or min(image.size) < 16 or max(image.size) > 4096:
                    raise ValueError(f"无效的 PNG: {name}")
                image.verify()
            with Image.open(path) as image:
                image.load()
            destination = payload / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            covered.add(identifier)
            count += 1
        coverage[folder] = {"records": len(ids), "covered": len(covered), "images": count, "missing_ids": sorted(ids - covered)}
    atomic_json(payload / "produce_resources_source.json", {
        "repository": REPOSITORY, "revision": revision, "source_fingerprint": fingerprint,
        "images_path": "packages/gakumas-images/images/{skillCards,pDrinks,pItems}/icons",
        "image_processing": "原始 PNG，未缩放或重新编码", "license": "GAKUMAS_DATA_LICENSE.txt",
        "coverage": coverage,
    })
    validate_catalogs(payload)
    files = [{"path": path.relative_to(payload).as_posix(), "size": path.stat().st_size, "sha256": file_hash(path)}
             for path in sorted(payload.rglob("*")) if path.is_file()]
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    version = hashlib.sha256(canonical).hexdigest()
    bundle = output / "full.zip"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for entry in files:
            info = zipfile.ZipInfo(entry["path"], date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (payload / entry["path"]).read_bytes())
    manifest = {
        "protocol": PROTOCOL, "version": version, "source_revision": revision, "source_fingerprint": fingerprint,
        "published_at": timestamp, "files": files,
        "bundle": {"size": bundle.stat().st_size, "sha256": file_hash(bundle)},
    }
    validate_manifest(manifest)
    atomic_json(output / "manifest.json", manifest)
    atomic_json(output / "latest.json", {"protocol": PROTOCOL, "version": version,
                                         "manifest_size": (output / "manifest.json").stat().st_size,
                                         "manifest_sha256": file_hash(output / "manifest.json")})
    print(json.dumps({"version": version, "coverage": coverage}, ensure_ascii=False, indent=2))
    return manifest


class Publisher:
    """R2 的写入仅在 Actions 使用；先上传不可变文件，最后切换指针。"""

    def __init__(self):
        import boto3

        self.bucket = os.environ["R2_BUCKET"]
        self.client = boto3.client("s3", endpoint_url=f"https://{os.environ['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                                   aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                                   region_name="auto")

    def get_json(self, key):
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            try:
                return json.loads(response["Body"].read())
            finally:
                response["Body"].close()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise

    def verified_upload(self, key, path, *, immutable=True):
        from botocore.exceptions import ClientError

        path = Path(path)
        digest = file_hash(path)
        if immutable:
            try:
                head = self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
                    raise
            else:
                if head["ContentLength"] != path.stat().st_size or head.get("Metadata", {}).get("sha256") != digest:
                    raise ValueError(f"已存在的不可变对象不一致: {key}")
                return
        content_type = "application/json" if key.endswith(".json") else "application/zip" if key.endswith(".zip") else "application/octet-stream"
        self.client.upload_file(str(path), self.bucket, key, ExtraArgs={
            "ContentType": content_type, "Metadata": {"sha256": digest},
            "CacheControl": "public, max-age=31536000, immutable" if immutable else "no-cache",
        })
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            actual = hashlib.sha256()
            for block in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                actual.update(block)
            if actual.hexdigest() != digest:
                raise ValueError(f"R2 上传后哈希不符: {key}")
        finally:
            response["Body"].close()

    def publish(self, output, manifest):
        output = Path(output)
        version = manifest["version"]
        # 同时复用内容寻址对象，避免每个版本重复上传全部图标。
        for entry in manifest["files"]:
            self.verified_upload(f"v1/objects/{entry['sha256']}", output / "payload" / entry["path"])
        self.verified_upload(f"v1/releases/{version}/full.zip", output / "full.zip")
        self.verified_upload(f"v1/releases/{version}/manifest.json", output / "manifest.json")
        self.verified_upload("v1/latest.json", output / "latest.json", immutable=False)
        # 发布完成后才清理；任何清理失败不影响已发布版本。
        self.prune(version)

    def rollback(self, version, output):
        manifest = self.get_json(f"v1/releases/{version}/manifest.json")
        validate_manifest(manifest)
        if manifest["version"] != version:
            raise ValueError("回退清单版本不一致")
        raw = self.client.get_object(Bucket=self.bucket, Key=f"v1/releases/{version}/manifest.json")["Body"]
        try:
            content = raw.read()
        finally:
            raw.close()
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "latest.json", {"protocol": PROTOCOL, "version": version, "manifest_size": len(content),
                                             "manifest_sha256": hashlib.sha256(content).hexdigest()})
        # 发布时已校验过不可变对象；回退前确认整包和每个独立对象仍存在。
        for key in [f"v1/releases/{version}/full.zip", *(f"v1/objects/{row['sha256']}" for row in manifest["files"])]:
            self.client.head_object(Bucket=self.bucket, Key=key)
        self.verified_upload("v1/latest.json", output / "latest.json", immutable=False)

    def prune(self, current):
        objects = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="v1/"):
            objects.extend(page.get("Contents", []))
        releases = sorted((row for row in objects if row["Key"].startswith("v1/releases/") and row["Key"].endswith("/manifest.json")),
                          key=lambda row: row["LastModified"], reverse=True)
        kept = {f"v1/releases/{current}/manifest.json", *(row["Key"] for row in releases[:14])}
        if len(kept) > 14:
            kept.remove(next(row["Key"] for row in reversed(releases) if row["Key"] in kept and f"/{current}/" not in row["Key"]))
        versions, hashes = set(), set()
        for key in kept:
            manifest = validate_manifest(self.get_json(key))
            versions.add(manifest["version"])
            hashes.update(row["sha256"] for row in manifest["files"])
        stale = [row["Key"] for row in objects if (
            row["Key"].startswith("v1/releases/") and row["Key"].split("/")[2] not in versions
            or row["Key"].startswith("v1/objects/") and row["Key"].split("/")[-1] not in hashes)]
        for start in range(0, len(stale), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in stale[start:start + 1000]]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--rollback", help="将最新版指针回退到保留版本")
    args = parser.parse_args()
    publisher = Publisher() if args.publish else None
    if args.rollback:
        if not publisher:
            parser.error("--rollback 需要 --publish")
        publisher.rollback(args.rollback, args.output)
        return
    if not args.source:
        parser.error("构建需要 --source")
    previous = None
    if publisher:
        latest = publisher.get_json("v1/latest.json")
        if latest:
            previous = publisher.get_json(f"v1/releases/{latest['version']}/manifest.json")
    manifest = build(args.source, args.output, previous)
    if manifest and publisher:
        publisher.publish(args.output, manifest)


if __name__ == "__main__":
    main()
