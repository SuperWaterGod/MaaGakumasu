from utils import logger
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("SocietyRequestAuto")
class SocietyRequestAuto(CustomRecognition):
    """
        自动选择社团请求物品
        选择数量最少的物品
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        reco_detail = context.run_recognition(
            "SocietyRequestChooseItem", argv.image,
            pipeline_override={"SocietyRequestChooseItem": {
                "recognition": "OCR",
                "expected": "^\\d{1,3}(,\\d{3})*$",
                "roi": [25, 400, 665, 500],
                "threshold": 0.9
            }})
        if reco_detail and reco_detail.hit:
            items_list = []
            for result in reco_detail.filtered_results:
                items_list.append({
                    "box": result.box,
                    "text": int(result.text.replace(",", ""))
                })
            sorted_list = sorted(items_list, key=lambda item: item['text'])
            min_item = sorted_list[0]
            logger.info(f"info: 已选择最少数量:{min_item['text']}")
            return CustomRecognition.AnalyzeResult(box=min_item["box"], detail={"detail": "选择数量最少的物品"})

        logger.error("err: OCR识别失败!")
        reco_detail = context.run_recognition("SocietyRequestChoose", argv.image,
                                              pipeline_override={"SocietyRequestChooseItem": {
                                                  "recognition": "FeatureMatch",
                                                  "template": "items/logic_yellow.png"
                                              }})
        best_result = reco_detail.best_result
        logger.debug("使用默认选项")
        return CustomRecognition.AnalyzeResult(box=best_result.box, detail={"detail": "OCR识别失败"})

