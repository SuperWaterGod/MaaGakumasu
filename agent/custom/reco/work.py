import json
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

        def recognize_smile(image, roi):
            return context.run_recognition("WorkChooseGood", image, pipeline_override={"WorkChooseGood": {"roi": roi}})

        def recognize_affinity(image):
            return context.run_recognition("WorkIdolAffinity", image, pipeline_override={"WorkIdolAffinity": {
                "recognition": "OCR",
                "expected": "^(?:0|[1-9]\\d?)/[1-9]\\d$",
                "roi": [70, 788, 558, 240]
            }})

        def recognize_work(image, box):
            return context.run_recognition("WorkAlready", image, pipeline_override={"WorkAlready": {
                "roi": [box[0] - 100, box[1] - 10, 150, 150]
            }})

        def handle_smile_page(page_image, roi, swipe_coords):
            smile_reco_detail = recognize_smile(page_image, roi)
            if smile_reco_detail and smile_reco_detail.hit:
                # 有笑脸
                good_list = [result.box for result in smile_reco_detail.filtered_results]
                new_page_image = context.tasker.controller.post_screencap().wait().get()
                work_reco_detail = recognize_work(new_page_image, good_list[0])
                if work_reco_detail and work_reco_detail.hit:
                    if len(good_list) > 1:
                        # 笑脸存在且被选中, 选择第二个笑脸
                        logger.info("已选择笑脸")
                        good_box = [good_list[1][0] - 50, good_list[1][1] + 50, good_list[1][2], good_list[1][3]]
                        return CustomRecognition.AnalyzeResult(box=good_box, detail={"detail": "笑脸存在且被选中，选择第二个笑脸"})
                    else:
                        # 笑脸存在且被选中
                        logger.debug("第二页")
                        context.tasker.controller.post_swipe(*swipe_coords, duration=200).wait()
                        time.sleep(1)
                else:
                    # 笑脸存在且未被选中
                    logger.info("已选择笑脸")
                    good_box = [good_list[0][0] - 50, good_list[0][1] + 50, good_list[0][2], good_list[0][3]]
                    return CustomRecognition.AnalyzeResult(box=good_box, detail={"detail": "笑脸存在且未被选中"})
            else:
                # 无笑脸
                logger.debug("返回第一页")
                context.tasker.controller.post_swipe(*swipe_coords, duration=200).wait()
                time.sleep(1)
            return None

        # 处理第一页笑脸
        first_result = handle_smile_page(argv.image, [8, 700, 621, 317], (400, 864, 200, 864))
        if first_result:
            return first_result

        # 处理第二页笑脸
        new_image = context.tasker.controller.post_screencap().wait().get()
        second_result = handle_smile_page(new_image, [104, 700, 615, 309], (200, 864, 400, 864))
        if second_result:
            return second_result

        # 处理好感度
        affinity_image = context.tasker.controller.post_screencap().wait().get()
        affinity_reco_detail = recognize_affinity(affinity_image)
        if affinity_reco_detail and affinity_reco_detail.hit:
            affinity_list = [{"box": result.box, "text": int(result.text.split('/')[0])} for result in affinity_reco_detail.filtered_results]
            sorted_list = sorted(affinity_list, key=lambda item: item['text'])
            max_affinity = sorted_list[-1]
            second_affinity = sorted_list[-2]
            new_affinity_image = context.tasker.controller.post_screencap().wait().get()
            work_reco_detail_ocr = recognize_work(new_affinity_image, max_affinity["box"])
            if work_reco_detail_ocr and work_reco_detail_ocr.hit:
                # 最高好感已工作
                logger.info("已选择可选的最高好感")
                return CustomRecognition.AnalyzeResult(box=second_affinity["box"], detail={"detail": "第二高好感度"})
            else:
                # 最高好感未工作
                logger.info("已选择可选的最高好感")
                return CustomRecognition.AnalyzeResult(box=max_affinity["box"], detail={"detail": "最高好感度"})
        else:
            logger.warning("OCR识别失败")
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "OCR识别失败"})


@AgentServer.custom_recognition("WorkChooseIdol")
class WorkChooseIdol(CustomRecognition):
    """
        选择指定Idol
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        idol = json.loads(argv.custom_recognition_param)["idol"]

        reco_detail = context.run_recognition(
            "WorkChooseIdolRecognition", argv.image,
            pipeline_override={"WorkChooseIdolRecognition": {
                "recognition": "TemplateMatch",
                "template": idol
            }})
        if reco_detail and reco_detail.hit:
            box = reco_detail.filtered_results[0].box
            return CustomRecognition.AnalyzeResult(box=box, detail={"detail": "已选中"})
        else:
            context.tasker.controller.post_swipe(400, 864, 200, 864, duration=200).wait()
            time.sleep(0.5)
            image = context.tasker.controller.post_screencap().wait().get()

            reco_detail = context.run_recognition(
                "WorkChooseIdolRecognition", image,
                pipeline_override={"WorkChooseIdolRecognition": {
                    "recognition": "TemplateMatch",
                    "template": idol
                }})
            if reco_detail and reco_detail.hit:
                box = reco_detail.filtered_results[0].box
                return CustomRecognition.AnalyzeResult(box=box, detail={"detail": "已选中"})

        logger.warning("未能找到指定Idol")
        return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "无文字"})
