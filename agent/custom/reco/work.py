import time
from utils import logger
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("WorkChooseAuto")
class WorkChooseAuto(CustomRecognition):
    """
        自动识别选择工作
        优先选择笑脸，没有笑脸则按照好感度选择
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        reco_detail = context.run_recognition("WorkChooseGood", argv.image,
                                              pipeline_override={"WorkChooseGood": {
                                                  "roi": [8, 709, 621, 317]
                                              }})
        if reco_detail:
            logger.info("第一页有笑脸")

            good_list = []
            for result in reco_detail.filterd_results:
                good_list.append(result.box)

            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition(
                "WorkAlready", image,
                pipeline_override={"WorkAlready": {
                    "roi": [good_list[0][0], good_list[0][1], 150, 150]
                }})

            if reco_detail:
                if len(good_list) > 1:
                    logger.info("笑脸存在且被选中, 选择第二个笑脸")
                    return CustomRecognition.AnalyzeResult(box=good_list[1], detail="笑脸存在且被选中, 选择第二个笑脸")
                else:
                    logger.info("笑脸存在且被选中, 左滑动")
                    context.tasker.controller.post_swipe(400, 864, 200, 864, duration=200).wait()
                    time.sleep(0.5)
            else:
                logger.info("笑脸存在且未被选中")
                return CustomRecognition.AnalyzeResult(box=good_list[0], detail="笑脸存在且未被选中")

        else:
            logger.info("第一页无笑脸, 左滑动")
            context.tasker.controller.post_swipe(400, 864, 200, 864, duration=200).wait()
            time.sleep(0.5)

        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("WorkChooseGood", image,
                                              pipeline_override={"WorkChooseGood": {
                                                  "roi": [104, 719, 615, 309]
                                              }})
        if reco_detail:
            logger.info("第二页有笑脸")

            good_list = []
            for result in reco_detail.filterd_results:
                good_list.append(result.box)
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition(
                "WorkAlready", image,
                pipeline_override={"WorkAlready": {
                    "roi": [good_list[0][0], good_list[0][1], 150, 150]
                }})
            if reco_detail:
                if len(good_list) > 1:
                    logger.info("笑脸存在且被选中, 选择第二个笑脸")
                    return CustomRecognition.AnalyzeResult(box=good_list[1], detail="笑脸存在且被选中, 选择第二个笑脸")
                else:
                    logger.info("笑脸存在且被选中, 右滑动")
                    context.tasker.controller.post_swipe(200, 864, 400, 864, duration=200).wait()
                    time.sleep(0.5)
            else:
                logger.info("笑脸存在且未被选中")
                return CustomRecognition.AnalyzeResult(box=good_list[0], detail="笑脸存在且未被选中")

        else:
            logger.info("第二页无笑脸，右滑动")
            context.tasker.controller.post_swipe(200, 864, 400, 864, duration=200).wait()
            time.sleep(0.5)
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition(
            "WorkIdolAffinity", image,
            pipeline_override={"WorkIdolAffinity": {
                "recognition": "OCR",
                "expected": "^(0|[1-9]|1[0-9]|20)\\/20$",
                "roi": [70, 788, 558, 240]}
            })

        if reco_detail:
            affinity_list = []
            for result in reco_detail.filterd_results:
                affinity_list.append({
                    "box": result.box,
                    "text": int(result.text.replace("/20", ""))
                })
            sorted_list = sorted(affinity_list, key=lambda item: item['text'])
            max_affinity = sorted_list[-1]
            second_affinity = sorted_list[-2]
            max_affinity_box = max_affinity["box"]

            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition(
                "WorkAlready", image,
                pipeline_override={"WorkAlready": {
                    "roi": [max_affinity_box[0] - 90, max_affinity_box[1] - 110, 150, 150]
                }})

            if reco_detail:
                logger.info("最高好感已工作")
                return CustomRecognition.AnalyzeResult(box=second_affinity["box"], detail="第二高好感度")
            else:
                logger.info("最高好感未工作")
                return CustomRecognition.AnalyzeResult(box=max_affinity["box"], detail="最高好感度")
        else:
            logger.info("OCR识别失败")
            return CustomRecognition.AnalyzeResult(box=None, detail="无文字")


"""
        # context is a reference, will override the pipeline for whole task
        context.override_pipeline({"MyCustomOCR": {"roi": [1, 1, 114, 514]}})
        # context.run_recognition ...


        # make a new context to override the pipeline, only for itself
        new_context = context.clone()
        new_context.override_pipeline({"MyCustomOCR": {"roi": [100, 200, 300, 400]}})
        reco_detail = new_context.run_recognition("MyCustomOCR", argv.image)

        click_job = context.tasker.controller.post_click(10, 20)
        click_job.wait()

        context.override_next(argv.node_name, ["TaskA", "TaskB"])
"""