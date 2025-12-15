import os
import sys
from loguru import logger


class LoggerWithPrint:
    """封装loguru logger"""

    # level映射关系: 格式化模板，{message}会被替换为实际消息
    LEVEL_MAP = {
        "TRACE": "trace: {message}",
        "DEBUG": "debug: {message}",
        "INFO": "info: {message}",
        "SUCCESS": "info: [color:green]{message}[/color]",
        "WARNING": "warn: {message}",
        "ERROR": "err: {message}",
        "CRITICAL": "critical: {message}",
    }

    def __init__(self, log_dir="debug/custom", print_levels=None):
        """
        初始化Logger

        Args:
            log_dir: 日志文件目录
            print_levels: 需要print的level列表，None表示全部打印
                         例如: ["INFO", "ERROR"] 只打印info和error
                         空列表 [] 表示不打印任何level
        """
        self.logger = self._setup_logger(log_dir)
        # 如果为None，默认打印所有level
        self.print_levels = set(lvl.upper() for lvl in print_levels) if print_levels is not None else None

    @staticmethod
    def _setup_logger(log_dir):
        os.makedirs(log_dir, exist_ok=True)

        logger.remove()

        logger.add(
            sys.stderr,
            format="[<level>{level}</level>] <level>{message}</level>",
            colorize=True,
            level="INFO",
        )

        logger.add(
            f"{log_dir}/{{time:YYYY-MM-DD}}.log",
            rotation="00:00",
            retention="2 weeks",
            compression="zip",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            encoding="utf-8",
            enqueue=True,
        )

        return logger

    def _log_with_print(self, level, message):
        """内部方法:记录日志并打印格式化信息"""
        # 获取对应的日志方法
        log_func = getattr(self.logger, level.lower())
        log_func(message)

        # 检查是否需要打印此level
        if self.print_levels is None or level.upper() in self.print_levels:
            # 获取格式化模板并替换message
            template = self.LEVEL_MAP.get(level.upper(), f"{level.lower()}: {{message}}")
            print_msg = template.format(message=message)

            # 打印格式化后的信息
            print(print_msg)

    def set_print_levels(self, levels):
        """
        动态设置需要print的level

        Args:
            levels: level列表，None表示全部打印，[]表示不打印
                   例如: ["INFO", "ERROR"]
        """
        self.print_levels = set(lvl.upper() for lvl in levels) if levels is not None else None

    def enable_print(self, level):
        """启用某个level的print"""
        if self.print_levels is None:
            self.print_levels = set(self.LEVEL_MAP.keys())
        self.print_levels.add(level.upper())

    def disable_print(self, level):
        """禁用某个level的print"""
        if self.print_levels is None:
            self.print_levels = set(self.LEVEL_MAP.keys())
        self.print_levels.discard(level.upper())

    def trace(self, message):
        self._log_with_print("TRACE", message)

    def debug(self, message):
        self._log_with_print("DEBUG", message)

    def info(self, message):
        self._log_with_print("INFO", message)

    def success(self, message):
        self._log_with_print("SUCCESS", message)

    def warning(self, message):
        self._log_with_print("WARNING", message)

    def error(self, message):
        self._log_with_print("ERROR", message)

    def critical(self, message):
        self._log_with_print("CRITICAL", message)

    # 提供原始logger的访问(如果需要使用其他高级特性)
    @property
    def raw_logger(self):
        return self.logger


# 创建全局logger实例
custom_logger = LoggerWithPrint(print_levels=["INFO", "ERROR", "SUCCESS", "WARNING", "CRITICAL"])
