from utils import logger
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ScreenRotateCheck")
class ScreenRotateCheck(CustomRecognition):
    """
        检测通过屏幕是否旋转判
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height = image.shape[0]
        width = image.shape[1]
        context.run_task("Click_1")
        if height < width:
            logger.debug("横屏")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "屏幕横屏"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕竖屏"})
