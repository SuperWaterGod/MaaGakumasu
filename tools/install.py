from pathlib import Path

import shutil
import sys
import json

import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(script_dir)

working_dir = Path(__file__).parent.parent
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"
platform_tag = len(sys.argv) > 2 and sys.argv[2] or ""


def install_deps(platform: str):
    """安装 MaaFramework 依赖到对应架构路径

    Args:
        platform: 平台标签，如 win-x64, linux-arm64, osx-arm64
    """
    if not platform:
        raise ValueError("platform_tag is required")

    print(f"Installing MaaFramework dependencies for platform: {platform}")

    # 将 Framework 的库文件复制到对应平台的 runtimes 目录
    shutil.copytree(
        working_dir / "deps" / "bin",
        install_path / "runtimes" / platform / "native",
        ignore=shutil.ignore_patterns(
            "*MaaDbgControlUnit*",
            "*MaaThriftControlUnit*",
            "*MaaWin32ControlUnit*",
            "*MaaRpc*",
            "*MaaHttp*",
        ),
        dirs_exist_ok=True,
    )

    # 复制 MaaAgentBinary
    shutil.copytree(
        working_dir / "deps" / "share" / "MaaAgentBinary",
        install_path / "MaaAgentBinary",
        dirs_exist_ok=True,
    )

    print(f"MaaFramework dependencies installed to runtimes/{platform}/native")


def install_resource():

    shutil.copytree(
        working_dir / "assets" / "resource",
        install_path / "resource",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        working_dir / "assets" / "interface.json",
        install_path,
    )

    # Copy lang folder for MFAAvalonia i18n support
    lang_src = working_dir / "assets" / "lang"
    if lang_src.exists():
        shutil.copytree(
            lang_src,
            install_path / "lang",
            dirs_exist_ok=True,
        )
        print("Copied lang folder for i18n support")

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = json.load(f)

    interface["version"] = version

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        json.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores():
    for file in ["README.md", "LICENSE", "requirements.txt"]:
        shutil.copy2(
            working_dir / file,
            install_path,
        )
    shutil.copytree(
        working_dir / "docs",
        install_path / "docs",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.yaml"),
    )


def install_agent():
    shutil.copytree(
        working_dir / "agent",
        install_path / "agent",
        dirs_exist_ok=True,
    )

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = json.load(f)

    if sys.platform.startswith("win"):
        interface["agent"]["child_exec"] = r"./python/python.exe"
    elif sys.platform.startswith("darwin"):
        interface["agent"]["child_exec"] = r"./python/bin/python3"
    elif sys.platform.startswith("linux"):
        interface["agent"]["child_exec"] = r"python3"

    interface["agent"]["child_args"] = ["-u", r"./agent/main.py"]

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        json.dump(interface, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    install_deps(platform_tag)
    install_resource()
    install_chores()
    install_agent()

    print(f"Install to {install_path} successfully.")
