import os
import re
import json
import time
import unicodedata
from typing import Tuple, Union, Optional
from difflib import SequenceMatcher

import numpy as np
from utils import logger
from maa.define import RectType
from maa.context import Context
from produce.state import read_produce_health
from produce.rewards import shop_exit_visible
from produce.sessions import SESSIONS
from produce.selection import debug_choice
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceRuleChoice")
class ProduceRuleChoice(CustomRecognition):
    """命中时返回当前页的点击框；未命中交给原 next 优先级。"""

    def analyze(self, context, argv):
        session = SESSIONS.get(context)
        if not session:
            return None
        capture = session["capture"]
        policy = capture.selection
        if not policy:
            return None
        if argv.node_name == "ProduceRuleShopDone":
            if session.get("shop_rule_active"):
                points = session.get("shop_balance")
                reason = session.get("shop_stop_reason") or (
                    f"规划完成，剩余 {points} P" if points is not None else "余额未读到，结束购买"
                )
                logger.info(f"商店 · {reason}")
                return [0, 0, 1, 1]
            shop = capture.shop
            if not shop or shop["points"]["value"] is None or not shop["candidates"]:
                return None
            points = shop["points"]["value"]
            if all(c["unavailable_reason"] == "sold_out" or (
                c["price"]["value"] is not None and c["price"]["value"] > points
            ) for c in shop["candidates"]):
                return [0, 0, 1, 1]
            return None
        if argv.node_name == "ProduceRuleShopWait":
            if not session.get("shop_rule_active"):
                return None
            return [0, 0, 1, 1]
        if argv.node_name == "ProduceRuleShopBlocked":
            if session.get("shop_rule_active") and (
                session.get("shop_shelf_blocked") or not shop_exit_visible(context, argv.image)
            ):
                return [0, 0, 1, 1]
            return None
        if argv.node_name == "ProduceRuleShopResumeDrinkFull":
            # 饮料满时商品仍被选中，详情会持续盖住货架提示。
            # 用饮料满提示和退出按钮确认已回到可继续规划的货架。
            if not session.get("shop_drinks_full") or not shop_exit_visible(context, argv.image):
                return None
            full = context.run_recognition("ProduceShoppingDrinkFull", argv.image)
            return [0, 0, 1, 1] if full and full.hit else None
        if argv.node_name in {"ProduceRuleShopAgain", "ProduceRuleShopDismiss"}:
            cached = session.pop("shop_shelf_visibility", None)
            can_dismiss = session.get("shop_dismiss_clicks", 0) < 4
            if argv.node_name == "ProduceRuleShopDismiss" and not can_dismiss:
                return None
            if cached and np.array_equal(cached[0], argv.image):
                _, title_visible, exit_visible = cached
            else:
                title = context.run_recognition("ProduceShoppingFlag", argv.image)
                title_visible = bool(title and title.hit)
                exit_visible = shop_exit_visible(context, argv.image)
            if argv.node_name == "ProduceRuleShopAgain":
                if title_visible and exit_visible:
                    return [0, 0, 1, 1]
                # 下一个关闭弹层节点沿用同帧结果。截图变化时重新识别。
                if can_dismiss:
                    session["shop_shelf_visibility"] = (argv.image, title_visible, exit_visible)
                return None
            if title_visible != exit_visible:
                return [480, 150, 1, 1]
            pending = session.get("shop_pending") or {}
            if not title_visible and pending.get("operation") == "buy" and pending.get("name"):
                # 成交浮层同时盖住货架提示和退出按钮，下方标题显示刚买的商品名。
                node = "ProduceShopStrengthenedText"
                receipt = context.run_recognition(node, argv.image, pipeline_override={
                    node: {"roi": [75, 842, 570, 60], "expected": [re.escape(pending["name"])]},
                })
                if receipt and receipt.hit:
                    return [480, 150, 1, 1]
            # 强化、删除的完成提示都可能遮住货架标记，只按当前服务的结果文案收尾。
            if not title_visible and pending.get("operation") in {"strengthen", "delete"}:
                node = "ProduceShopStrengthenedText"
                override = {node: {"expected": ["を削除しました", "已[删刪]除"]}} if pending["operation"] == "delete" else {}
                receipt = context.run_recognition(node, argv.image, pipeline_override=override)
                if receipt and receipt.hit:
                    return [480, 150, 1, 1]
            return None
        if argv.node_name == "ProduceShopByRule":
            choice = self.confirm_shop_step(context, argv.image, session)
            if context.tasker.stopping:
                return None
            if "shop_balance" not in session and capture.shop:
                session["shop_balance"] = capture.shop["points"]["value"]
                policy.health = read_produce_health(context, argv.image)
            if not choice and not session.get("shop_stop_reason"):
                choice = self.shop(session)
            if choice:
                session["shop_choice"] = choice
            elif not session.get("shop_rule_active"):
                reason = "货架或余额未读到" if session.get("shop_balance") is None else "没有命中规则的可购商品或服务"
                logger.info(f"商店－默认 · {reason}")
            debug_choice(capture, context, argv.image, argv.node_name, choice)
        else:
            policy.health = read_produce_health(context, argv.image)
            page = session.get("reward_page")
            if not page:
                return None
            choice = policy.choose(page["candidates"], "receive")
            if choice:
                name = choice.get("display_name") or choice.get("base_name") or policy.catalog.display_name(choice["catalog_id"])
                session["rule_reward"] = {"reason": choice["reason"], "item_name": name}
            elif not page.get("fallback_reason"):
                has_identity = any(candidate.get("catalog_id") or candidate.get("base_name") for candidate in page["candidates"])
                page["fallback_reason"] = "已识别候选未命中流派规则" if has_identity else "候选名称未确认"
            debug_choice(capture, context, argv.image, "receive", choice)
        if choice:
            x, y, w, h = choice["box"]
            return CustomRecognition.AnalyzeResult(box=[x + w // 2, y + h // 2, 1, 1], detail={"reason": choice["reason"]})
        return None

    @staticmethod
    def confirm_shop_step(context, image, session):
        pending = session.get("shop_pending")
        if not pending:
            return
        node = "ProduceRecognitionPoint"
        detail = context.run_recognition(node, image, pipeline_override={node: {"expected": r"^[0-9]+(?:,[0-9]{3})*$"}})
        if context.tasker.stopping:
            return
        result = detail.best_result if detail and detail.hit else None
        actual = int(result.text.replace(",", "")) if result and result.score >= .8 else None
        expected = pending["balance"] - pending["price"]
        session.pop("shop_pending")
        target = pending.pop("edit_target", None)
        if actual == expected:
            session["shop_balance"] = actual
            policy = session["capture"].selection
            if pending["operation"] == "buy":
                policy.acquired(pending["catalog_id"], pending.get("base_name"))
            elif target is not None:
                policy.apply_edit(pending["operation"], target["catalog_id"], target["base_name"])
        elif (actual == pending["balance"] and session.get("shop_drinks_full")
              and pending["operation"] == "buy" and pending["attempt"][1] >= 4):
            session["shop_balance"] = actual
        elif (actual == pending["balance"] and session.get("shop_drinks_full")
              and pending["operation"] == "strengthen" and not pending.get("retried_after_drink_full")
              and shop_exit_visible(context, image)):
            full = context.run_recognition("ProduceShoppingDrinkFull", image)
            if full and full.hit:
                logger.debug("商店 · 饮料栏已满，余额未变且仍在货架，补点强化入口")
                return {**pending, "retried_after_drink_full": True}
            session["shop_stop_reason"] = "成交余额未核实，结束购买"
        else:
            session["shop_stop_reason"] = "成交余额未核实，结束购买"

    @staticmethod
    def shop(session):
        capture = session["capture"]
        shop = capture.shop
        points = session.get("shop_balance")
        if not shop or points is None:
            return None
        used = session.get("shop_used", set())
        policy = capture.selection
        choices = []
        shelf = []
        for index, item in enumerate(shop["candidates"]):
            price = item["price"]["value"]
            if item["available"] is not True or price is None or price > points or ("buy", index) in used:
                continue
            identity = item["identity"] or {}
            if session.get("shop_drinks_full") and index >= 4:
                continue
            shelf.append(identity)
            grade, reason = policy.evaluate_candidate(identity, "buy")
            if grade:
                name = identity.get("display_name") or identity["database_name"]
                choices.append({"operation": "buy", "attempt": ("buy", index), "catalog_id": identity["catalog_id"], "name": name,
                                "base_name": identity.get("base_name"), "balance": points, "grade": grade, "price": price,
                                "reason": "购买" + name + "，" + reason, "box": item["box"]})
        for service in shop["services"]:
            kind = service["operation"]
            if kind in used or not 0 < service["price"] <= points:
                continue
            cards = [{"catalog_id": key} for key in sorted(policy.deck_copies) if policy.deck_copies[key] > 0]
            known = policy.family_counts(policy.deck_copies)
            cards.extend({"base_name": name} for name, count in policy.deck_families.items() if count > known[name])
            best = policy.choose(cards, kind, shop_estimate=True)
            if best:
                action = "强化" if kind == "strengthen" else "删除"
                choices.append({**service, "attempt": kind, "balance": points, "grade": best["grade"],
                                "reason": f"拟{action}，预估收益：{best['reason']}"})
        if not choices:
            capped = any(policy.at_copy_target(c.get("catalog_id"), c.get("base_name")) for c in shelf)
            if capped or (policy.build and shelf and all(c.get("catalog_id") in policy.rows for c in shelf)):
                # 首次进店也须拦住流水线的打折商品兜底，避免重新点击已排除的商品。
                session["shop_rule_active"] = True
                session["shop_stop_reason"] = "同名牌已达目标张数，无其他合适商品或服务" if capped else "已识别的商品与服务均无需购买"
        return max(choices, key=lambda c: (c["grade"], -c["price"]), default=None)


@AgentServer.custom_recognition("ProduceChooseIdolAuto")
class ProduceChooseIdolAuto(CustomRecognition):
    """
    自动识别当前偶像名称和歌曲
    """

    # 文本相似度阈值，低于该值视为识别失败
    SIMILARITY_THRESHOLD = 0.7

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        idol_name = json.loads(argv.custom_recognition_param)["idol_name"]
        song_name = json.loads(argv.custom_recognition_param)["song_name"]
        recognized_name = ""
        recognized_song = ""

        true_end_detail = context.run_recognition(
            "ProduceChooseIdolTrueEnd",
            argv.image,
            pipeline_override={
                "ProduceChooseIdolTrueEnd": {
                    "recognition": "OCR",
                    "expected": ["True", "End"],
                    "roi": [430, 34, 266, 48],
                }
            },
        )
        if true_end_detail and true_end_detail.hit:
            logger.debug("识别到True End")
            idol_name_roi = [440, 128, 280, 64]
            song_name_roi = [380, 90, 320, 45]
        else:
            logger.debug("未识别到True End")
            idol_name_roi = [400, 98, 320, 64]
            song_name_roi = [340, 60, 380, 45]

        name_detail = context.run_recognition(
            "ProduceChooseIdolName",
            argv.image,
            pipeline_override={"ProduceChooseIdolName": {"recognition": "OCR", "roi": idol_name_roi}},
        )
        name_score = 0.0
        if name_detail and name_detail.hit:
            recognized_name = "".join([item.text for item in name_detail.all_results]).replace(" ", "")
            name_score = self.similarity_ratio(recognized_name, idol_name)
            logger.info(f"识别到偶像名称: {recognized_name}，相似度: {name_score:.2f}")

        song_detail = context.run_recognition(
            "ProduceChooseIdolSong",
            argv.image,
            pipeline_override={"ProduceChooseIdolSong": {"recognition": "OCR", "roi": song_name_roi}},
        )
        song_score = 0.0
        if song_detail and song_detail.hit:
            recognized_song = "".join([item.text for item in song_detail.all_results]).replace("[", "").replace("]", "")
            song_score = self.song_similarity_ratio(recognized_song, song_name)
            logger.info(f"识别到歌曲名称: {recognized_song}，相似度: {song_score:.2f}")

        if name_score >= self.SIMILARITY_THRESHOLD and song_score >= self.SIMILARITY_THRESHOLD:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={
                "detail": "识别偶像卡成功", "observed_idol": recognized_name, "observed_song": recognized_song,
                "configured_idol": idol_name, "configured_song": song_name,
                "idol_similarity": name_score, "song_similarity": song_score,
            })
        else:
            logger.debug(f"识别偶像卡失败，偶像相似度: {name_score:.2f}，歌曲相似度: {song_score:.2f}")
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "识别偶像卡失败"})

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        归一化 OCR 文本，抹平特殊符号带来的差异

        OCR 对特殊符号识别不稳定，例如「36℃ U･B･U」常被识别成「36°C U・B・U」，
        直接比较相似度只有 0.63，会导致该偶像卡无法被识别。
        NFKC 会先把全角转半角、把 ℃ 拆成 °C、把 ･ 转成 ・，
        之后只保留字母、数字、假名与汉字，符号与空格一律忽略。
        """
        return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isalnum())

    @staticmethod
    def similarity_ratio(str1, str2):
        """返回0-1之间的相似度分数，1表示完全相同（比较前先归一化，忽略符号差异）"""
        return SequenceMatcher(
            None,
            ProduceChooseIdolAuto.normalize_text(str1),
            ProduceChooseIdolAuto.normalize_text(str2),
        ).ratio()

    @classmethod
    def song_similarity_ratio(cls, recognized_song: str, song_name: str) -> float:
        """歌曲名完整包含时忽略皮肤附加文案，否则沿用相似度比较。"""
        recognized = cls.normalize_text(recognized_song)
        configured = cls.normalize_text(song_name)
        if not recognized or not configured:
            return 0.0
        if configured in recognized:
            return 1.0
        return SequenceMatcher(None, recognized, configured).ratio()


@AgentServer.custom_recognition("ProduceShowStart")
class ProduceShowStart(CustomRecognition):
    """
    检测通过屏幕是否旋转判断演出开始
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height, width = image.shape[0], image.shape[1]
        if height < width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceShowEnd")
class ProduceShowEnd(CustomRecognition):
    """
    检测通过屏幕是否旋转判断演出结束
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height, width = image.shape[0], image.shape[1]
        if height > width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceCardsFlagAuto")
class ProduceCardsFlagAuto(CustomRecognition):
    """
    自动识别出牌场景
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        context.run_action("Click_1")
        cards_reco_detail = context.run_recognition("ProduceRecognitionCards", argv.image)
        health_reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", argv.image)
        if cards_reco_detail and cards_reco_detail.hit and health_reco_detail and health_reco_detail.hit:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别到出牌场景"})
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到选择场景"})
