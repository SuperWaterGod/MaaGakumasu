import json
from utils import logger

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context


class Counter:
    def __init__(self, max_count: int = 0):
        self.count = 0
        self.max = max_count

    def init(self, max_count: int = 0):
        self.count = 0
        self.max = max_count

    def increment(self):
        self.count += 1
        return self.count

    def is_max(self):
        if self.max <= 0:
            return False
        return self.count >= self.max

    def get_count(self):
        return self.count

    def set_max(self, max_count: int):
        self.max = max_count


class CounterManager:
    def __init__(self):
        self.counters = {}

    def get(self, name: str = "default") -> Counter:
        if name not in self.counters:
            self.counters[name] = Counter()
        return self.counters[name]

    def remove(self, name: str):
        if name in self.counters:
            del self.counters[name]

    def reset(self, name: str, max_count: int = 0):
        self.counters[name] = Counter(max_count)


counter_manager = CounterManager()


@AgentServer.custom_action("InitCounter")
class InitCounter(CustomAction):
    def run(
            self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        try:
            # 解析 JSON 字符串参数
            params = json.loads(argv.custom_action_param) if argv.custom_action_param else {}
            name = params.get("name", "default")
            max_count = params.get("max", 0)

            counter_manager.reset(name, max_count)
            logger.debug(f"初始化计数器成功: name={name}, max={max_count}")
            return True
        except Exception as e:
            logger.error(f"初始化计数器失败: {e}")
            return False


@AgentServer.custom_action("UseCounter")
class UseCounter(CustomAction):
    def run(
            self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult | bool:
        try:
            # 解析 JSON 字符串参数
            params = json.loads(argv.custom_action_param) if argv.custom_action_param else {}
            name = params.get("name", "default")
            text = params.get("text", "")

            # 即使没有 init_counter,也能直接使用
            counter = counter_manager.get(name)

            # 如果参数中有 max,则更新最大次数
            if "max" in params:
                counter.set_max(params["max"])

            counter.increment()
            current = counter.get_count()

            if text:
                logger.debug(f"计数器 '{name}' 第{current}次{text}")

            if counter.is_max():
                logger.debug(f"计数器 '{name}' 已达到最大次数: {counter.max}")
                return False

            return True
        except Exception as e:
            logger.error(f"计数失败: {e}")
            return False
