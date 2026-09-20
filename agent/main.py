import os
import re
import sys
import json
import subprocess
from typing import NamedTuple, Optional
from pathlib import Path

# 获取当前main.py所在路径并设置上级目录为工作目录
current_file_path = os.path.abspath(__file__)
current_dir = os.path.dirname(current_file_path)
parent_dir = os.path.dirname(current_dir)
os.chdir(parent_dir)
# print(f"设置工作目录为: {parent_dir}")

# 将当前目录添加到路径
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from utils import logger
except ImportError:
    # 如果logger不存在，创建一个简单的logger
    import logging

    logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
    logger = logging


def read_pip_config() -> dict:
    """
    读取 pip 配置文件并返回配置字典
    """
    config_dir = Path("./config")
    config_dir.mkdir(exist_ok=True)

    config_path = config_dir / "pip_config.json"
    default_config = {
        "enable_pip_update": True,
        "enable_pip_install": True,
        "last_version": "unknown",
        "mirror": "https://mirrors.ustc.edu.cn/pypi/simple",
        "backup_mirrors": [
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            "https://mirrors.cloud.tencent.com/pypi/simple/",
            "https://pypi.org/simple",
        ],
    }

    if not config_path.exists():
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=4)
        return default_config

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception("读取pip配置失败，使用默认配置")
        return default_config


def get_available_mirror(pip_config: dict | None) -> Optional[str]:
    """
    检查镜像源可用性并返回一个可用的镜像源
    """
    if pip_config is None:
        return None
    mirrors = [pip_config.get("mirror")] + pip_config.get("backup_mirrors", [])
    for mirror in mirrors:
        try:
            logger.info(f"尝试连接镜像源: {mirror}")
            response = subprocess.run(
                [sys.executable, "-m", "pip", "list", "-i", mirror],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if response.returncode == 0:
                logger.info(f"镜像源可用: {mirror}")
                return mirror
        except Exception:
            logger.warning(f"镜像源不可用: {mirror}")
    logger.error("所有镜像源都不可用")
    return None


def install_requirements(req_file="requirements.txt", pip_config=None) -> bool:
    """
    安装 requirements.txt 中的依赖
    """
    req_path = Path(req_file)
    if not req_path.exists():
        logger.error(f"requirements.txt 不存在")
        return False

    # 获取可用的镜像源
    mirror = get_available_mirror(pip_config)
    if not mirror:
        logger.error("没有可用的镜像源，安装依赖失败")
        return False

    try:
        logger.info("开始安装依赖...")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-U",
            "-r",
            str(req_path),
            "--no-warn-script-location",
            "-i",
            mirror,
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                if "Collecting" in line:
                    pkg = line.replace("Collecting", "").strip()
                    logger.info(f"正在安装: {pkg}")
                elif "Downloading" in line:
                    pkg = line.replace("Downloading", "").strip().split()[0]
                    logger.info(f"下载: {pkg}")
                elif "Installing collected packages" in line:
                    pkg = line.replace("Installing collected packages:", "").strip()
                    logger.info(f"安装完成: {pkg}")
        process.wait()
        if process.returncode == 0:
            logger.info("依赖安装完成")
            return True
        else:
            logger.error("依赖安装失败")
            return False
    except Exception as e:
        logger.exception("pip 安装依赖时出错")
        return False


def update_pip(pip_config=None):
    """
    更新 pip 到最新版本
    """
    mirror = get_available_mirror(pip_config)
    if not mirror:
        logger.error("没有可用的镜像源，无法更新 pip")
        return False

    try:
        logger.info("正在更新 pip...")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "--no-warn-script-location",
            "-i",
            mirror,
        ]

        subprocess.check_call(cmd)
        logger.info("pip 更新成功")
        return True
    except Exception as e:
        logger.exception("更新 pip 时出错")
        return False


def check_and_install_dependencies():
    """
    检查并安装依赖
    """
    pip_config = read_pip_config()
    enable_pip_update = pip_config.get("enable_pip_update", True)
    enable_pip_install = pip_config.get("enable_pip_install", True)

    current_version = read_interface_version()
    last_version = pip_config.get("last_version", "unknown")

    logger.info(f"启用 pip 安装依赖: {enable_pip_install}")
    logger.info(f"当前版本: {current_version}, 上次运行版本: {last_version}")

    need_install = enable_pip_install and (current_version != last_version or current_version == "unknown")
    if not need_install:
        logger.info("依赖已就绪，跳过依赖检查与 pip 更新")
        return

    if enable_pip_update:
        if not update_pip(pip_config=pip_config):
            logger.warning("pip 更新失败，继续尝试安装依赖...")

    if install_requirements(pip_config=pip_config):
        update_pip_config(current_version)
        logger.info("依赖检查完成")
    else:
        logger.warning("依赖安装失败，程序可能无法正常运行")


def read_interface_version(interface_file="./interface.json") -> str:
    """
    读取 interface.json 文件中的版本信息
    """
    candidates = [
        Path(interface_file),
        Path("./interface.json"),
        Path("./assets/interface.json"),
    ]

    for path in candidates:
        if not path.exists():
            continue

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("version", "unknown")
        except Exception:
            logger.exception("读取 interface.json 版本失败")
            return "unknown"

    logger.warning("interface.json 不存在")
    return "unknown"


def update_pip_config(version) -> bool:
    """
    更新 pip 配置文件中的版本信息
    """
    config_path = Path("./config/pip_config.json")
    try:
        config = read_pip_config()
        config["last_version"] = version

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        return True
    except Exception as e:
        logger.exception("更新pip配置失败")
        return False


def compact_traceback() -> str:
    """取异常栈的末尾几帧拼成一行，便于在 UI 日志面板中直接看到出错位置"""
    import traceback

    frames = traceback.extract_tb(sys.exc_info()[-1])
    tail = frames[-4:]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno}" for f in tail)


LOG_MARKER = "MAA Process Start"


def read_log_tail(path: Path, mark_count: int = 2, chunk: int = 64 * 1024) -> str:
    """从文件尾部由后向前读取，直到读到 mark_count 个进程启动段

    日志文件是历次运行累加的，可能很大，因此不整体读入；而单次运行的日志量也可能
    超过一个固定窗口（本次运行产生了大量 TRACE 时尤其明显），所以按块回退读取，
    直到拿到足够多的段为止。读取失败返回空串。
    """
    marker = LOG_MARKER.encode()
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            while size > 0:
                step = min(chunk, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
                if data.count(marker) >= mark_count:
                    break
    except Exception:
        return ""

    return data.decode("utf-8", errors="replace")


class FrameworkVersion(NamedTuple):
    """某个 MaaFramework 进程记录的版本与进程号"""

    version: str
    pid: str


class LogBlock(NamedTuple):
    """MaaFramework 日志中一个进程的启动段"""

    pid: str
    start: int
    lines: list[str]


def parse_log_blocks(lines: list[str]) -> list[LogBlock]:
    """按 ``MAA Process Start`` 把日志切成一个个进程启动段

    日志由各进程共享写入，每个进程启动时都会写一段以 ``[Logger] MAA Process Start``
    开头的记录，紧跟其后的一行即该进程所用的 MaaFramework 版本。
    """
    starts = [index for index, line in enumerate(lines) if LOG_MARKER in line]
    blocks = []

    for order, start in enumerate(starts):
        end = starts[order + 1] if order + 1 < len(starts) else len(lines)
        match = re.search(r"\[Px(\w+)\]", lines[start])
        blocks.append(LogBlock(match.group(1) if match else "", start, lines[start:end]))

    return blocks


def read_ui_framework_version(
    log_path: Path = Path("./debug/maafw.log"), pid: Optional[str] = None
) -> Optional[FrameworkVersion]:
    """从 maafw.log 中读取 UI 侧 MaaFramework 的版本

    MaaFramework 的日志由各进程共享写入，每个进程启动时都会写一段

        [Logger] MAA Process Start
        [Logger] Version v5.12.2

    本进程（agent）作为 UI 的子进程，总是在 UI 之后启动，所以日志中位于本进程
    那一段之前、最靠近它的一段就是 UI 侧，它的 ``Version`` 即 UI 所用 MaaFramework
    的版本。找不到时返回 None，由调用方退化为只提示不报错。
    """
    pid = pid if pid is not None else str(os.getpid())

    lines = read_log_tail(log_path).splitlines()
    if not lines:
        return None

    blocks = parse_log_blocks(lines)
    own = [block for block in blocks if block.pid == pid]
    if not own:
        return None

    # 本进程是最后启动的，取最后一段，避免与旧运行中进程号相同的那一段混淆
    own_start = own[-1].start
    ui = max((block for block in blocks if block.start < own_start), key=lambda block: block.start, default=None)
    if ui is None:
        return None

    version = next(
        (m.group(1) for line in ui.lines if (m := re.search(r"\[Logger\] Version (v[\w.+\-]+)", line))),
        None,
    )
    if version is None:
        return None

    return FrameworkVersion(version, ui.pid)


def _hint_ui_version() -> str:
    """读不到 UI 版本时的兜底提示"""
    return "若连接失败，通常是 maafw 版本与 UI 内置的 MaaFramework 版本不一致，请重新下载完整压缩包解压使用。"


def check_framework_version(logger, library_version: str) -> None:
    """比对 agent 侧与 UI 侧的 MaaFramework 版本，不一致时给出明确报错

    agent 侧是 pip 包 ``maafw`` 自带的 MaaAgentServer，UI 侧是 MFAAvalonia 打包的
    MaaFramework。两者协议版本号（kProtocolVersion）不同时，服务端会在握手阶段
    以 "Protocol version mismatch" 拒绝连接，UI 端只会显示「连接失败」，
    因此这里主动读日志比对并提示。
    """
    ui = read_ui_framework_version()
    if ui is None:
        logger.info(f"本机 maafw 版本: {library_version}")
        logger.info(_hint_ui_version())
        return

    if ui.version == library_version:
        logger.info(f"maafw 版本与 UI 一致: {library_version}")
        return

    logger.error(
        f"maafw 版本与 UI 内置的 MaaFramework 不一致，Agent 将无法连接:\n"
        f"    UI 侧 (MaaFramework, Px{ui.pid}): {ui.version}\n"
        f"    agent 侧 (maafw):                {library_version}\n"
        f"  两侧通信协议版本不同，握手会被服务端拒绝，UI 只会提示「连接失败」。\n"
        f"  修复: 重新下载完整压缩包解压使用（或经 Mirror 酱更新），\n"
        f"        不要直接覆盖替换旧版本文件，避免残留旧资源导致识别异常。"
    )


def start_agent_server(logger, library_version: str) -> bool:
    """启动 AgentServer 并返回是否成功

    ``MaaAgentServerStartUp`` 失败时返回 False，此时继续 ``join`` 会一直阻塞
    等待一个不会到来的连接，UI 也只能超时，所以必须中断。
    """
    from maa.agent.agent_server import AgentServer

    socket_id = sys.argv[-1]

    try:
        started = AgentServer.start_up(socket_id)
    except Exception:
        logger.error(f"AgentServer 启动异常: {type(sys.exc_info()[1]).__name__}: {sys.exc_info()[1]}")
        logger.error(f"  出错位置: {compact_traceback()}")
        logger.error(f"  maafw 版本: {library_version}")
        logger.error(_hint_ui_version())
        return False

    if not started:
        logger.error(
            "AgentServer 启动失败，socket 标识未被接受，Agent 无法连接 UI\n"
            f"    socket 标识: {socket_id}\n"
            f"    maafw 版本: {library_version}"
        )
        logger.error(_hint_ui_version())
        return False

    logger.info("AgentServer 启动")
    AgentServer.join()
    AgentServer.shut_down()
    logger.info("AgentServer 关闭")
    return True


def report_failure(logger, title: str, *advice: str) -> None:
    """把失败原因与排查建议写到日志，便于在 UI 日志面板中直接看到"""
    error = sys.exc_info()[1]
    logger.error(f"{title}: {type(error).__name__}: {error}")
    logger.error(f"  出错位置: {compact_traceback()}")
    for line in advice:
        logger.error(f"  {line}")


def agent():
    try:
        from utils import logger
    except Exception:
        import traceback

        traceback.print_exc()
        raise

    try:
        # 先于 custom 导入：custom 内部也依赖 maa，第三方包的问题要单独区分出来
        from maa.library import Library
        from maa.toolkit import Toolkit
    except Exception:
        report_failure(
            logger,
            "agent 依赖加载失败",
            f"当前 Python: {sys.executable}",
            "修复: 重新下载完整压缩包解压使用（或经 Mirror 酱更新），不要覆盖替换旧文件",
            "源码运行时可执行:",
            f"  {sys.executable} -m pip install -U -r requirements.txt",
        )
        raise

    try:
        # custom 通过装饰器注册自定义识别/动作，导入即可，不需要引用
        import custom  # noqa: F401
    except Exception:
        report_failure(logger, "自定义逻辑加载失败")
        raise

    try:
        Toolkit.init_option("./")
    except Exception:
        report_failure(logger, "MaaFramework 初始化失败", _hint_ui_version())
        raise

    library_version = Library.version()
    logger.info(f"maafw Library version: {library_version}")
    check_framework_version(logger, library_version)

    start_agent_server(logger, library_version)


def main():
    check_and_install_dependencies()
    agent()


if __name__ == "__main__":
    main()
