from utils import logger
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("WorkChooseAuto")
class WorkChooseAuto(CustomRecognition):
    """
        识别选择工作
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        logger.info("开始识别")
        reco_detail = context.run_recognition(
            "WorkIdolAffinity",
            argv.image,
            pipeline_override={"WorkIdolAffinity":
                {
                    "recognition": "OCR",
                    "expected": "^(?:0|[1-9]|1[0-9]|20)20$",
                    "roi": [70, 788, 558, 240],
                    "threshold": 0.3
                }
            },
        )
        if reco_detail is None:
            logger.info("识别失败")
            return CustomRecognition.AnalyzeResult(box=None, detail="无文字")
        else:
            affinity_list = []
            for result in reco_detail.filterd_results:

                affinity_level = 0
                logger.info(result)
                if len(result.text) < 3:
                    continue
                elif len(result.text) == 3:
                    affinity_level = result.text.replace("20", "")
                elif len(result.text) == 4:
                    affinity_level = result.text.replace("120", "")
                else:
                    continue
                affinity_list.append({
                    "box": result.box,
                    "text": affinity_level
                })
            max_affinity = max(affinity_list, key=lambda item: int(item['text']))
            print(max_affinity)
        """# context is a reference, will override the pipeline for whole task
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
        return CustomRecognition.AnalyzeResult(
            box=(0, 0, 100, 100), detail="Hello World!"
        )
