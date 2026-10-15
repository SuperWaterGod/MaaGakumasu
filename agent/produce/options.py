"""培育options动作；流水线注册名称与原行为保持不变。"""

import json
import time
import random
from typing import Any, Dict, List, Optional

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ProduceChooseWorkAuto")
class ProduceChooseWorkAuto(CustomAction):
    """
    处理选择工作类型的窗口

    选择逻辑：
    - 体力 > 10 时：选择会扣除体力对应的工作（health_position 指定的位置）
      - 单个位置 [2] 或 [3]：选择对应的工作
      - 无位置或多位置 [2,3]：随机选择
    - 体力 <= 10 时：选择不扣除体力对应的工作
      - [2] → 选择第三个（位置3不扣体力）
      - [3] → 选择第二个（位置2不扣体力）
      - 其他情况 → 选择第一个
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        first_roi = [100, 760]
        second_roi = [100, 880]
        third_roi = [100, 1000]
        image = context.tasker.controller.post_screencap().wait().get()
        health_data = self._get_health(context, image)
        current = health_data["current"] if health_data else 0

        health_position = self._get_health_position(context, image)

        if current > 10:
            if health_position and len(health_position) == 1:
                box = second_roi if health_position[0] == 2 else third_roi
            else:
                box = random.choice([first_roi, second_roi, third_roi])
        else:
            if health_position == [2]:
                box = third_roi
            elif health_position == [3]:
                box = second_roi
            else:
                box = first_roi
        context.tasker.controller.post_click(box[0], box[1]).wait()
        return True

    @staticmethod
    def _get_health(context: Context, image) -> Optional[dict]:
        """获取当前体力数值"""
        reco_detail = context.run_recognition("ProduceRecognitionHealth", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            health_parts = reco_detail.best_result.text.split("/")
            current_health = int("".join(filter(str.isdigit, health_parts[0])))
            max_health = int("".join(filter(str.isdigit, health_parts[1])))
            ratio = current_health / max_health
            logger.info(f"体力: {current_health}/{max_health} ({ratio:.2%})")
            return {"current": current_health, "max": max_health, "ratio": ratio}
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_health_position(context: Context, image) -> Optional[list]:
        """
        获取扣除体力的图标位置

        Returns:
            None: 未检测到扣体力图标
            [2]: 第二个位置扣体力
            [3]: 第三个位置扣体力
            [2,3]: 第二和第三个位置都扣体力
        """
        reco_detail = context.run_recognition(
            "ProduceRecognitionHealthFlag",
            image,
            pipeline_override={"ProduceRecognitionHealthFlag": {"roi": [370, 830, 320, 290]}},
        )
        if reco_detail.hit:
            position = []
            for result in reco_detail.filtered_results:
                health_box_y = result.box[1]
                if health_box_y > 970:
                    position.append(3)
                else:
                    position.append(2)
            return position
        return None


@AgentServer.custom_action("ProduceChooseOptionsAuto")
class ProduceChooseOptionsAuto(CustomAction):
    """
    处理选择选项的窗口

    根据当前得分自动选择属性选项：
    1. 获取第一、第二、第三属性的当前分数
    2. THIRD_ATTR_PROBABILITY 概率选择第三属性，剩余概率选择第二属性
    3. 在可用选项中执行双击选择
    4. 若目标选项不可用，按优先级 fallback：null > 第二属性 > 第三属性 > 随机
    """

    OPTIONS_CONFIG = {
        "Vo": "produce/choose_Vo.png",
        "Da": "produce/choose_Da.png",
        "Vi": "produce/choose_Vi.png",
        "null": "produce/choose_null.png",
    }
    CLICK_DELAY = 0.5
    THIRD_ATTR_PROBABILITY = 0.6  # 选择第三属性的概率，剩余概率选择第二属性

    def __init__(self):
        super().__init__()
        self.first = "Vi"
        self.second = "Da"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        """
        执行自动选择选项

        Returns:
            bool: 选择成功返回 True，否则返回 False
        """
        preference = (
            context.get_node_data("ProduceChooseNIAEventFlag")
            .get("action", {})
            .get("param", {})
            .get("custom_action_param", {"first": "Vo", "second": "Vi"})
        )
        self.first = preference["first"]
        self.second = preference["second"]
        image = context.tasker.controller.post_screencap().wait().get()
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        options = self._get_available_options(context, image)

        # 计算选择
        first_score = score.get(self.first, 0)
        second_score = score.get(self.second, 0)
        # 计算第三属性（Vo、Da、Vi 中非 first/second 的那个）
        all_attrs = ["Vo", "Da", "Vi"]
        third = next(attr for attr in all_attrs if attr != self.first and attr != self.second)
        third_score = score.get(third, 0)
        logger.info(f"第一属性 {self.first}={first_score}, 第二属性 {self.second}={second_score}, 第三属性 {third}={third_score}")

        # 判断逻辑：根据 THIRD_ATTR_PROBABILITY 概率选择第三属性，剩余概率选择第二属性
        if random.random() < self.THIRD_ATTR_PROBABILITY:
            choice = third
            logger.debug(f"随机数 < {self.THIRD_ATTR_PROBABILITY}，选择第三属性: {choice}")
        else:
            choice = self.second
            logger.debug(f"随机数 >= {self.THIRD_ATTR_PROBABILITY}，选择第二属性: {choice}")

        # 找到目标选项
        target_box = None
        for opt in options:
            if choice in opt:
                target_box = opt[choice]
                break

        # 如果目标选项不可用，尝试 fallback（null 优先）
        if target_box is None:
            logger.debug(f"选项 {choice} 不可用，尝试 fallback")
            # 按 null > 第二属性 > 第三属性 > 随机的优先级选择
            priority = ["null", self.second, third, None]
            for p in priority:
                if p is None:
                    # 最后从所有可用选项中随机选择
                    if options:
                        random_opt = random.choice(options)
                        choice = next(iter(random_opt.keys()))
                        target_box = random_opt[choice]
                        logger.debug(f"Fallback 随机选择: {choice}")
                    break
                for opt in options:
                    if p in opt:
                        target_box = opt[p]
                        choice = p
                        logger.debug(f"Fallback 选择 {p}")
                        break
                if target_box:
                    break

        # 执行双击
        if target_box:
            # box 格式为 [x, y, w, h]，计算中心点
            center_x = target_box[0] + target_box[2] // 2
            center_y = target_box[1] + target_box[3] // 2
            self._double_click(context, center_x, center_y)
            logger.info(f"已选择选项: {choice}, 坐标: ({center_x}, {center_y})")
        else:
            logger.warning("没有可用选项，无法选择")
            return True  # 没有选项可选时默认返回True，避免卡死在这里
        return True

    @staticmethod
    def _get_current_score(context: Context, image) -> Optional[dict]:
        """获取当前得分"""
        score = {
            "Vo": 0,
            "Da": 0,
            "Vi": 0,
            "max": 0,
        }
        roi0_list = [[70 + i * 230, 430, 136, 80] for i in range(3)]
        roi1_list = [[150 + i * 150, 325, 136, 80] for i in range(3)]
        first_reco = context.run_recognition(
            "ProduceRecognitionScore",
            image,
            pipeline_override={"ProduceRecognitionScore": {"roi": roi0_list[0]}},
        )
        use_roi_list = roi1_list if not (first_reco and first_reco.hit) else roi0_list

        for i in range(3):
            reco_detail = context.run_recognition(
                "ProduceRecognitionScore",
                image,
                pipeline_override={"ProduceRecognitionScore": {"roi": use_roi_list[i]}},
            )
            if reco_detail and reco_detail.hit and len(reco_detail.filtered_results) == 2:
                current_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[0].text)))
                max_score = int("".join(filter(lambda c: c.isdigit(), reco_detail.filtered_results[1].text.replace("/", ""))))
                logger.debug(f"第{i + 1}列得分: {current_score} / {max_score}")
                score[["Vo", "Da", "Vi"][i]] = current_score
                score["max"] = max_score if score["max"] < max_score < 9999 else score["max"]
        try:
            logger.info(f"当前得分: Vo={score['Vo']}, Da={score['Da']}, Vi={score['Vi']}, Max={score['max']}")
            return score
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    def _get_available_options(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用选项列表"""
        available_options = []
        available_options_name = ""

        for option_name, option_img in self.OPTIONS_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceRecognitionWorkOptions",
                image,
                pipeline_override={"ProduceRecognitionWorkOptions": {"template": option_img, "focus": None}},
            )
            if reco_detail and reco_detail.hit:
                available_options.append({option_name: reco_detail.best_result.box})
                available_options_name += f"{option_name}, "

        logger.info(f"可用选项: {available_options_name.rstrip(', ')}")
        return available_options

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


@AgentServer.custom_action("ProduceChooseMirrorAuto")
class ProduceChooseMirrorAuto(CustomAction):
    """
    自动选择试镜挑战难度

    根据当前投票数在对应试镜的阈值列表中选择满足条件的最高难度：
    - 获取当前投票数和试镜各门槛分数及坐标
    - 选取满足 vote >= threshold 的最高门槛
    - 根据降档配置先跳过指定档数，再检测锁定，锁定则继续降档
    - 降档至 threshold=0 或找到未锁定选项为止
    """

    CLICK_DELAY = 0.5

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        vote = self._get_current_vote(context, image) or 1
        mirror = self._get_current_mirror(context, image)
        lowering_difficulty = self._get_lowering_difficulty(context, argv)

        # 门槛分数降序排列
        thresholds = sorted((int(k) for k in mirror.keys()), reverse=True)

        # 找到满足当前投票的最高门槛索引
        start_idx = 0
        for i, threshold in enumerate(thresholds):
            if vote >= threshold:
                start_idx = i
                break

        # 先降指定档数，再检查锁定，锁定则继续降档直至成功或为0档
        target_threshold = 0
        x = y = 0
        start = min(start_idx + lowering_difficulty, len(thresholds) - 1)
        for i in range(start, len(thresholds)):
            target_threshold = thresholds[i]
            target_box = mirror[str(target_threshold)]
            x = target_box[0] + target_box[2] // 2
            y = target_box[1] + target_box[3] // 2

            if target_threshold == 0:
                break
            if not self._check_lock(context, image, target_box):
                break

            logger.info(f"分数 {target_threshold:,} 已锁定，继续降档")

        # 在点击前判断当前是第几镜，若启用 focus 则点击后进入手动接管
        mirror_idx = self._get_focus_mirror_index(context, image)

        logger.info(f"当前投票: {vote:,}, 目标分数: {target_threshold:,}, 点击坐标: ({x}, {y - 20})")
        self._double_click(context, x, y - 20)

        if not mirror_idx:
            return True

        logger.info(f"检测到 focus 配置，进入手动接管模式（第{mirror_idx}镜）")
        context.run_action(
            "ProduceMirrorFocus",
            pipeline_override={
                "ProduceMirrorFocus": {
                    "focus": {
                        "Node.ActionNode.Succeeded": {
                            "content": f"[color:LimeGreen]已暂停自动培育进入手动接管状态（第{mirror_idx}镜）[/color]\n"
                            '退出手动接管请点击下方"确定"按钮',
                            "display": ["log", "modal", "notification"],
                        }
                    }
                }
            },
        )

        return True

    @staticmethod
    def _get_focus_mirror_index(context: Context, image) -> int:
        """获取当前试镜对应的 focus 镜号。"""
        node_data = context.get_node_data("ProduceMirrorFlag")
        if not node_data:
            return 0
        attach = node_data.get("attach", {})
        logger.debug(f"focus 配置: {attach}")
        # 仅当有 focus_x 为 true 时才识别
        has_focus = any(attach.get(f"focus_{i}", False) for i in range(1, 4))
        if not has_focus:
            return 0

        for i in range(1, 4):
            reco_detail = context.run_recognition(
                f"ProduceMirrorFlag_{i}",
                image,
                pipeline_override={
                    f"ProduceMirrorFlag_{i}": {
                        "recognition": "TemplateMatch",
                        "roi": [12, 630, 696, 530],
                        "template": f"produce/NIA/mirror_{i}.png",
                        "threshold": 0.9,
                    }
                },
            )
            if reco_detail and reco_detail.hit:
                return i if attach.get(f"focus_{i}", False) else 0
        return 0

    @staticmethod
    def _check_lock(context: Context, image, target_box) -> bool:
        """检查目标分数附近是否有锁定图标"""
        roi = [
            target_box[0] + 330,
            target_box[1] - 80,
            100,
            100,
        ]
        reco_detail = context.run_recognition(
            "ProduceRecognitionLock",
            image,
            pipeline_override={
                "ProduceRecognitionLock": {
                    "recognition": "TemplateMatch",
                    "roi": roi,
                    "template": "produce/lock.png",
                    "green_mask": True,
                }
            },
        )
        return reco_detail is not None and reco_detail.hit

    @staticmethod
    def _get_lowering_difficulty(context: Context, argv) -> int:
        """检查是否启动降档"""
        params = json.loads(argv.custom_action_param)
        lowering_level = params.get("level") if isinstance(params, dict) else None
        if not lowering_level:
            return 0
        else:
            logger.info(f"检测到启动降档，降低{lowering_level}档")
            return int(lowering_level)

    @staticmethod
    def _get_current_mirror(context: Context, image):
        """获取当前试镜"""
        mirror = {"0": [360, 1050, 1, 1]}
        reco_detail = context.run_recognition("ProduceRecognitionMirror", image)
        if reco_detail and reco_detail.hit:
            for result in reco_detail.filtered_results:
                box = result.box
                text = "".join(filter(lambda c: c.isdigit(), result.text.replace(",", "")))
                mirror[text] = [box[0], box[1], box[2], box[3]]
        mirror = dict(sorted(mirror.items(), key=lambda x: int(x[0])))
        logger.info(f"当前试镜门槛分数: {list(mirror.keys())}")
        return mirror

    @staticmethod
    def _get_current_vote(context: Context, image) -> Optional[int]:
        """获取当前投票"""
        reco_detail = context.run_recognition("ProduceRecognitionVote", image)
        if reco_detail and reco_detail.hit:
            try:
                vote = int(reco_detail.best_result.text.replace(",", ""))
                return vote
            except ValueError:
                logger.warning("投票数据解析失败")
                return None

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


@AgentServer.custom_action("ProduceKeepDrinkAuto")
class ProduceKeepDrinkAuto(CustomAction):
    """
    处理保留饮料的窗口
    原本是在ProduceButton节点直接按保留按钮处理
    但是需要处理只有2瓶饮料时，一次性获得2瓶饮料时不会自动勾选3瓶的特殊情况
    所以转为独立处理
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceRecognitionUncheckedMark", image)
        if reco_detail.hit:
            for result in reco_detail.filtered_results:
                box = result.box
                context.tasker.controller.post_click(box[0] + int(box[2] / 2), box[1] + int(box[3] / 2)).wait()
                time.sleep(0.1)
            context.run_task("ProduceDrinkNoButton")
        return True
