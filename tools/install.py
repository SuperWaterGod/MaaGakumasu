import os
import sys
import json
import shutil
from pathlib import Path

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(script_dir)

working_dir = Path(__file__).parent.parent
install_path = working_dir / Path("install")
version = len(sys.argv) > 1 and sys.argv[1] or "v0.0.1"
platform_tag = len(sys.argv) > 2 and sys.argv[2] or ""
maafw_version = len(sys.argv) > 3 and sys.argv[3] or None


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
    shutil.copytree(
        working_dir / "assets" / "tasks",
        install_path / "tasks",
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

    # Copy lang folder for MFAAvalonia data support
    lang_src = working_dir / "assets" / "data"
    if lang_src.exists():
        shutil.copytree(
            lang_src,
            install_path / "data",
            dirs_exist_ok=True,
        )
        print("Copied data folder for data support")

    with open(install_path / "interface.json", "r", encoding="utf-8") as f:
        interface = json.load(f)

    interface["version"] = version

    with open(install_path / "interface.json", "w", encoding="utf-8") as f:
        json.dump(interface, f, ensure_ascii=False, indent=4)


def install_chores(maafw_version=None):
    for file in ["README.md", "LICENSE", "logo.ico"]:
        shutil.copy2(
            working_dir / file,
            install_path,
        )

    if maafw_version:
        # 发布构建时固定 maafw 版本，使其与打包的 MaaFramework natives 严格一致
        with open(working_dir / "requirements.txt", encoding="utf-8") as src, open(
            install_path / "requirements.txt", "w", encoding="utf-8", newline="\n"
        ) as dst:
            for line in src:
                stripped = line.strip()
                if stripped == "maafw" or stripped.startswith("maafw=="):
                    dst.write(f"maafw=={maafw_version}\n")
                else:
                    dst.write(line)
    else:
        shutil.copy2(
            working_dir / "requirements.txt",
            install_path,
        )
    shutil.copytree(
        working_dir / "docs",
        install_path / "docs",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.yaml"),
    )

    # 复制默认配置模板，MFAAvalonia 首次启动时会自动将其转换为 config.json
    config_dir = install_path / "config"
    config_dir.mkdir(exist_ok=True)
    shutil.copy2(
        working_dir / "config.template.json",
        config_dir / "config.template.json",
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
    install_chores(maafw_version)
    install_agent()

    print(f"Install to {install_path} successfully.")
