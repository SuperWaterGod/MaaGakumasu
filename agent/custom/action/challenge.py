import json
import random
from utils import logger

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ChallengeAuto")
class ChallengeAuto(CustomAction):
    """
    自动运行挑战
    """

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        params = json.loads(argv.custom_action_param)

        mode_allowed = ["fixed", "random", "auto", "max", "min"]
        mode = params.get("mode", "fixed")
        if mode not in mode_allowed:
            mode = "fixed"
            logger.warning(f"挑战参数错误, 仅允许{mode_allowed}, 已重置为fixed")

        if mode == "fixed":
            index = params.get("index", 1)
            index_allowed = [0, 1, 2]
            if index not in index_allowed:
                index = 1
                logger.warning(f"挑战参数错误, 仅允许{index_allowed}, 已重置为 1")
            logger.info(f"固定选择挑战第 {index + 1} 位")
            context.run_task("ChallengeIndex", pipeline_override={"ChallengeIndex": {"recognition": {"param": {"index": index}}}})

        elif mode == "random":
            index = random.choice([0, 1, 2])
            logger.info(f"随机选择挑战第 {index + 1} 位")
            context.run_task("ChallengeIndex", pipeline_override={"ChallengeIndex": {"recognition": {"param": {"index": index}}}})

        elif mode in ["auto", "max", "min"]:
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition("ChallengeRating", image, pipeline_override={"ChallengeRating": {
                "recognition": "OCR",
                "expected": "^\\d{3,6}$",
                "roi": [54, 634, 233, 447],
                "order_by": "Vertical"
            }})
            if reco_detail and reco_detail.hit:
                ratings = [result.text for result in reco_detail.filtered_results]
                index = 1
                logger.info(f"挑战评分识别结果: {ratings}")
                ratings_int = []
                for rating in ratings:
                    try:
                        ratings_int.append(int(rating))
                    except ValueError:
                        ratings_int.append(-1)
                if all(rating == -1 for rating in ratings_int):
                    index = 1
                    logger.warning("挑战评分识别失败, 已重置为第2位")
                elif mode == "max":
                    index = ratings_int.index(max(ratings_int))
                    logger.info(f"选择挑战评分最高的第 {index + 1} 位")
                elif mode == "min":
                    index = ratings_int.index(min(ratings_int))
                    logger.info(f"选择挑战评分最低的第 {index + 1} 位")
                else:  # auto
                    reco_detail = context.run_recognition("ChallengeRating", image, pipeline_override={"ChallengeRating": {
                        "recognition": "OCR",
                        "expected": "^\\d{3,6}$",
                        "roi": [172, 505, 195, 69],
                        "order_by": "Vertical"
                    }})
                    if reco_detail and reco_detail.hit:
                        self_rating_score = reco_detail.best_result.text
                        try:
                            self_rating_int = int(self_rating_score)
                            diffs = [abs(rating - self_rating_int) if rating != -1 else float('inf') for rating in ratings_int]
                            index = diffs.index(min(diffs))
                            logger.info(f"自动选择挑战第 {index + 1} 位")
                        except ValueError:
                            index = 1
                            logger.warning("自身挑战评分识别失败, 已重置为第2位")
                context.run_task("ChallengeIndex", pipeline_override={"ChallengeIndex": {"recognition": {"param": {"index": index}}}})
        else:
            logger.error("挑战参数错误, 已退出挑战")
        return True
