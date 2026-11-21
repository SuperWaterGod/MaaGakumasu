"""将单轮证据转换成可直接打开的培育回放。"""

import os
from html import escape
from string import Template
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from dataclasses import field, dataclass
from urllib.parse import quote

LABELS = {"score": "分数", "rank": "评级", "stamina": "体力", "genki": "元气", "turns_remaining": "剩余回合",
          "attribute": "属性", "attribute_multiplier_percent": "属性倍率", "health": "体力", "points": "P 点",
          "Vo": "声乐", "Da": "舞蹈", "Vi": "形象", "vote": "票数"}
REASONS = {"game_recommendation": "游戏推荐", "only_available_card": "仅一张可用牌",
           "timeout_detector_confidence": "等待推荐超时，使用兜底选择", "no_available_cards": "没有可用牌",
           "existing_use_all_drinks": "使用现有饮料", "no_cards_ocr": "未发现手牌"}
BATTLE_ACTIONS = {"play_card": "出牌", "use_drink_task": "使用饮料", "skip_round": "跳过回合", "move_cards": "移动卡牌"}
ADJUSTMENTS = {"delete": "删除", "copy": "复制", "exchange": "变换", "strengthen": "强化"}
SHOP_STEPS = {"ProduceShoppingBuySale": "选择折扣商品", "ProduceShoppingBuy": "点击购买",
              "ProduceShoppingDecide": "确认购物", "ProduceShoppingDrinkFull": "饮料已满", "ProduceShoppingExit": "离开商店",
              "ProduceChooseStrengthenFlag": "打开强化选牌", "ProduceChooseStrengthenClick": "选择强化对象",
              "ProduceChooseStrengthen": "确认强化", "ProduceChooseDeleteFlag": "选择删除对象",
              "ProduceChooseDeleteClick": "点击删除", "ProduceShoppingDeleteConfirm": "确认付费删除"}
EXAM_FAILURES = {"ProduceFailedFlag": "考试未通过", "ProduceNIAFailedFlag": "试镜未通过"}
EXAM_RETRIES = {"ProduceFailedRetryConfirm": "重试考试", "ProduceNIAFailedRetryConfirm": "重试试镜"}
LIFECYCLE = {"produce_session_start": ("开始", "开始记录"), "produce_session_resume": ("继续", "继续培育")}
FLOW_ICONS = {
    "开始": '<path d="M5 21V4m0 1c5-4 9 4 15 0v10c-6 4-10-4-15 0"/>',
    "领奖": '<path d="M3 9h18v4H3zm2 4v8h14v-8M12 9v12m0-12H9a2.5 2.5 0 1 1 2.5-2.5zm0 0h3a2.5 2.5 0 1 0-2.5-2.5z"/>',
    "事件": '<path d="M12 20v-7M4 7h13l3 3-3 3H4l3-3z"/>',
    "培育": '<path d="M6 7l-3 1 2 12 9-2m-4-15 10 2-2 13-10-2z"/>',
    "属性": '<path d="M4 20h16M7 16v-4m5 4V8m5 8V4"/>',
    "商店": '<path d="M3 9h18l-2-5H5zm1 0v11h16V9M10 20v-6h4v6"/>',
    "购买": '<path d="m3 4 2 1 3 10h10l3-8H6m3 12h.01m8 0h.01"/>',
    "强化": '<path d="m6 10 6-6 6 6m-6-6v16"/>',
    "删除": '<path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/>',
    "复制": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "变换": '<path d="M4 8h15m-4-4 4 4-4 4m5 4H5m4-4-4 4 4 4"/>',
    "工作": '<rect x="3" y="8" width="18" height="13" rx="2"/><path d="M8 8V4h8v4M3 13h18"/>',
    "考试": '<path d="M6 3h8l4 4v14H6zm8 0v5h4M9 12h6m-6 4h6"/>',
    "重试": '<path d="M20 11a8 8 0 1 0-1 5M20 5v6h-6"/>',
    "继续": '<path d="m9 5 10 7-10 7zM5 5v14"/>',
    "结算": '<path d="M5 12l4 4L19 6"/><circle cx="12" cy="12" r="10"/>',
}


@dataclass(frozen=True)
class ReplayChoice:
    label: str
    selected: bool = False


@dataclass
class ReplayEvent:
    category: str
    title: str
    time: str | None
    facts: list[tuple[str, str | list[ReplayChoice] | dict[str, str]]] = field(default_factory=list)
    pictures: list[tuple[str, dict]] = field(default_factory=list)
    operations: list["ReplayEvent"] = field(default_factory=list)
    subjects: tuple[str, ...] = ()
    input_steps: list[str] = field(default_factory=list)


def display(value):
    return "未知" if value is None else str(value)


def local_clock(value, zone):
    try:
        return datetime.fromisoformat(value).astimezone(zone).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return "时间未知"


def route_name(value, sp=False, *, course=True):
    if value.endswith("_SP"):
        value, sp = value[:-3], True
    if value in ("Vo", "Da", "Vi"):
        return f"{LABELS[value]}{' SP' if sp else ''}{'课程' if course else ''}"
    return {"rest": "休息", "null": "无属性选项"}.get(value, value)


def observation_changes(observations, last_values):
    """记录相邻读数差异，供选择前的详情使用。"""
    readings = {}

    def visit(key, value):
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
        if value is None:
            return
        if isinstance(value, dict):
            if "current" in value:
                value = f"{display(value['current'])}/{display(value.get('max'))}"
            else:
                for child, item in value.items():
                    visit(child, item)
                return
        if key in LABELS:
            previous = last_values.get(key)
            if previous is None:
                readings[LABELS[key]] = str(value)
            elif previous != value:
                readings[LABELS[key]] = f"{previous} → {value}"
            last_values[key] = value

    for key, value in observations.items():
        visit(key, value)
    return readings


def battle_operation(row, following):
    detail = row["detail"]
    operation = detail.get("operation")
    box = detail.get("selected_box")
    hand = detail.get("cards") or []
    chosen = next((card.get("name") for card in hand if box is not None and card.get("box") == box), None)
    action = BATTLE_ACTIONS.get(operation, "战斗操作")
    subjects = (chosen,) if operation == "play_card" and chosen else ()
    title = "" if subjects else "牌名未识别" if operation == "play_card" else action
    facts = []
    if detail.get("reason") in REASONS:
        facts.append(("选择依据", REASONS[detail["reason"]]))
    if hand:
        facts.append(("当时手牌", [ReplayChoice(card.get("name") or "未识别", box is not None and card.get("box") == box)
                                  for card in hand]))
    later = (following or {}).get("detail") or {}
    if later.get("framework_action_succeeded") is False:
        facts.append(("执行", "点击失败"))
    changes = {LABELS[key]: f"{display(change.get('before'))} → {display(change.get('after'))}"
               for key, change in (later.get("state_changes") or {}).items() if key in LABELS and isinstance(change, dict)}
    facts.append(("操作后观测", changes or "未确认"))
    return ReplayEvent(action, title, row["evidence"]["time"], facts, [("操作前截图", row["evidence"])], subjects=subjects)


def shop_purchase(row, following):
    detail = row["detail"]
    purchase = detail.get("shop_purchase") or {}
    name = purchase.get("name")
    confirmed_input = (following or row)["detail"].get("framework_action_succeeded")
    facts = [("结果", "点击失败" if confirmed_input is False else "购买结果未核验")]
    if purchase.get("price") is not None:
        facts.append(("标价", f"{purchase['price']} P 点"))
    pictures = [("购买前截图", row["evidence"])]
    return ReplayEvent("购买", "" if name else "商品未识别", row["evidence"]["time"], facts, pictures, subjects=(name,) if name else ())


def shop_facts(shelf):
    from .console import icon_name

    shop = shelf["detail"].get("shop") or {}
    if not shop:
        return []
    goods = []
    for item in shop.get("candidates") or []:
        price = (item.get("price") or {}).get("value")
        price_text = f"{price} P 点" if price is not None else "价格未知"
        goods.append(ReplayChoice(f"{icon_name(item.get('identity') or {})}（{price_text}）"))
    return [("入口 P 点", display((shop.get("points") or {}).get("value"))),
            ("入口可见商品", goods or "未识别")]


def settlement_event(rows, final):
    """合并结算期间的重复读数，按结果页与属性页各保留一份对应的截图证据。"""
    if not rows:
        return None
    facts = []
    pictures = []
    for title, keys in (("最终评价", ("rank", "score")), ("最终属性", ("Vo", "Da", "Vi"))):
        readings = {key: (final.get(key) or {}).get("value") for key in keys}
        if not any(value is not None for value in readings.values()):
            continue
        facts.append((title, {LABELS[key]: display(value) for key, value in readings.items()}))
        best_match = 0
        photo = None
        for row in rows:
            evidence = row["evidence"]
            if not (evidence.get("frame_path") or evidence.get("frame_missing_reason")):
                continue
            observed = row["detail"].get("settlement") or {}
            matches = sum(value is not None and observed.get(key) == value for key, value in readings.items())
            if matches > best_match:
                best_match, photo = matches, evidence
        if photo:
            pictures.append((title + "截图", photo))
    return ReplayEvent("结算", "培育结算", rows[0]["evidence"]["time"], facts, pictures)


def project_events(report):
    """流程节点与正文共用此顺序；操作结果关联到发起事件。"""
    from .console import icon_name

    timeline = report["timeline"]
    by_id = {row["evidence"]["event_id"]: row for row in timeline}
    after = {row["detail"]["decision_id"]: row for row in timeline
             if row["kind"] in ("decision_after", "produce_event_input_result", "route_input_result") and row["detail"].get("decision_id")}
    reward_names = defaultdict(dict)
    for row in timeline:
        if row["kind"] == "reward_candidate_preview":
            detail = row["detail"]
            reward_names[detail.get("reward_page_id")][(detail.get("candidate") or {}).get("candidate_id")] = detail.get("observed_name")

    last_values = {}
    events = []
    settlement_rows = []
    final = report["final_settlement"]
    final_sources = {item["event_id"] for field in final.values() for item in field.get("observations", []) if item.get("event_id")}
    battle = battle_key = None
    last_adjustment = adjustment_source = last_failure = None
    shop = shelf = shop_run = None
    shop_exiting = False
    battle_count = shop_count = 0
    session = report["session"]
    zone = datetime.fromisoformat(session.get("local_started_at") or session["started_at"]).tzinfo

    def finish_shop():
        nonlocal shop, shelf, shop_exiting, shop_count, adjustment_source
        if shop is None:
            return
        if shop.operations or shelf:
            shop_count += 1
            shop.title = f"商店 {shop_count}"
            if shelf:
                shop.facts.extend(shop_facts(shelf))
                shop.pictures.append(("本次进店货架", shelf["evidence"]))
            events.append(shop)
        shop = shelf = adjustment_source = None
        shop_exiting = False

    for row in timeline:
        kind, detail, evidence = row["kind"], row["detail"], row["evidence"]
        node, event_time = detail.get("node") or "", evidence["time"]
        following = after.get(evidence["event_id"])
        if shop is not None and evidence["run_id"] != shop_run:
            finish_shop()
        if kind in ("battle_invocation_start", "battle_invocation_end", "battle_exit"):
            battle = battle_key = None
            continue
        if kind == "decision_before":
            finish_shop()
            key = (evidence["run_id"], detail.get("battle_id") or evidence["event_id"])
            if battle is None or battle_key != key:
                battle_count += 1
                battle = ReplayEvent("培育", f"战斗 {battle_count}", event_time)
                battle_key = key
                events.append(battle)
            battle.operations.append(battle_operation(row, following))
            continue
        routed = kind in ("route_before", "pipeline_action")
        if kind == "shop_entry_observation" or (routed and node == "ProduceShoppingFlag"):
            if shop_exiting:
                finish_shop()
            if shop is None:
                shop = ReplayEvent("商店", "", event_time)
                shop_run = evidence["run_id"]
            if kind == "shop_entry_observation" and shelf is None:
                shelf = row
            battle = battle_key = None
            continue

        if routed and node == "ProduceShoppingBuy" and (shop is None or shop_exiting):
            # 兼容只记录了购买、缺少入口观察的历史会话。
            finish_shop()
            shop = ReplayEvent("商店", "", event_time)
            shop_run = evidence["run_id"]
        if shop is not None and routed and node in SHOP_STEPS and (not shop_exiting or node == "ProduceShoppingExit"):
            result = (following or row)["detail"].get("framework_action_succeeded")
            status = "输入完成" if result is True else "输入失败" if result is False else "结果未记录"
            shop.input_steps.append(f"{local_clock(event_time, zone)} · {SHOP_STEPS[node]} · {status}")
            if node == "ProduceShoppingExit" and result is not False:
                shop_exiting = True
        if node.startswith("ProduceShopping"):
            if node == "ProduceShoppingBuy" and routed:
                shop.operations.append(shop_purchase(row, following))
                battle = battle_key = None
            elif node == "ProduceShoppingDeleteConfirm" and routed and shop and not shop_exiting and shop.operations:
                operation = shop.operations[-1]
                if operation.category in {"删除", "卡牌调整"}:
                    operation.category = "删除"
                    operation.pictures.append(("付费删除确认页", evidence))
            continue
        if kind == "settlement_observation" or (kind == "exam_result_observation" and evidence["event_id"] in final_sources):
            if any(value is not None for value in (detail.get("settlement") or {}).values()):
                if kind == "settlement_observation":
                    finish_shop()
                settlement_rows.append(row)
            continue

        facts = []
        pictures = [("操作前截图", evidence)]
        subjects = ()
        adjustment_identity = None
        in_shop = False
        if kind in LIFECYCLE:
            category, title = LIFECYCLE[kind]
        elif kind == "produce_event_choice":
            category = "事件"
            option = (detail.get("selected") or {}).get("name")
            title = "选择" if option else "选项未识别"
            if option:
                subjects = (route_name(option),)
        elif kind == "option_choice":
            category = {"属性选择": "属性", "工作选择": "工作"}.get(detail.get("title"), "事件")
            title = detail.get("action") or "发送选择输入"
            if category == "属性" and title.startswith("点击 "):
                subjects = (route_name(title.removeprefix("点击 "), course=False),)
                title = "选择"
            elif detail.get("title") == "试镜选择" and title.startswith("点击门槛 "):
                subjects = (title.removeprefix("点击门槛 "),)
                title = "选择门槛"
            if detail.get("title") and category == "事件":
                facts.append(("操作类型", detail["title"]))
        elif kind == "reward_receive_input":
            page_id = detail.get("reward_page_id")
            choice = detail.get("policy_choice") or {}
            category = "领奖"
            if choice.get("policy_node") == "ProduceChooseSkip":
                title = "跳过奖励"
                selected = None
            else:
                selected = detail.get("selected_candidate_id")
                name = reward_names[page_id].get(selected) or choice.get("item_name")
                title = "" if name else "奖励未识别"
                if name:
                    subjects = (name,)
            choices = [ReplayChoice(name or "未识别", candidate_id is not None and candidate_id == selected)
                       for candidate_id, name in reward_names[page_id].items()]
            facts.append(("奖励候选", choices or "未识别"))
            if reason := choice.get("reason"):
                facts.append(("选择依据", reason))
            page = (report.get("reward_pages") or {}).get(page_id, {})
            start = (page.get("start") or {}).get("event") or {}
            if start.get("event_id") in by_id:
                pictures.insert(0, ("奖励总览", by_id[start["event_id"]]["evidence"]))
        elif kind == "route_before" and (adjustment := detail.get("card_adjustment")):
            if shop_exiting:
                finish_shop()
            if adjustment.get("stage") != "confirm":
                last_adjustment = None
                if node in {"ProduceChooseStrengthenFlag", "ProduceChooseDeleteFlag"} or detail.get("icon_matches"):
                    adjustment_source = row
                continue
            category = ADJUSTMENTS.get(adjustment.get("operation"), "卡牌调整")
            in_shop = shop is not None and category in {"强化", "删除", "卡牌调整"}
            target, upgraded = adjustment.get("target"), adjustment.get("upgraded_preview")
            if target and category != "卡牌调整":
                result = (following or row)["detail"].get("framework_action_succeeded")
                adjustment_identity = (evidence["run_id"], category, target, result)
                visible = shop.operations if in_shop else events
                # 同一次选牌可能反复命中确认按钮，按业务操作展示第一条。
                if last_adjustment and visible and last_adjustment[0] == adjustment_identity and visible[-1] is last_adjustment[1]:
                    continue
            subjects = tuple(name for name in (target, upgraded) if name)
            title = "" if subjects else "目标卡牌未识别"
            if upgraded and not target:
                facts.append(("强化后预览", upgraded))
            if adjustment_source and adjustment_source["evidence"]["run_id"] == evidence["run_id"]:
                observed = adjustment_source["detail"]
                names = [icon_name(hit) for hit in observed.get("icon_matches") or []]
                names = names or [name for name in observed.get("visible_candidates") or [] if name]
                if names:
                    facts.append(("选牌页识别结果", [ReplayChoice(name) for name in names]))
                    if any(hit.get("variant_verified") is False for hit in observed.get("icon_matches") or []):
                        facts.append(("符号说明", "≈：卡名已确认，强化状态未确认"))
                    pictures.insert(0, ("选牌页截图", adjustment_source["evidence"]))
            facts.append(("结果", "实际效果未核验"))
        elif kind == "exam_failure_or_retry" and node in EXAM_FAILURES:
            failure = (evidence["run_id"], battle_count, node)
            if failure == last_failure:
                continue
            last_failure = failure
            category, title = "考试", EXAM_FAILURES[node]
            pictures = [("考试结果截图", evidence)]
        elif kind == "pipeline_action" and node in EXAM_RETRIES and detail.get("framework_action_succeeded") is True:
            category, title = "重试", EXAM_RETRIES[node]
            if events and events[-1].title == title:
                continue
            last_failure = None
        else:
            if kind == "produce_session_end":
                finish_shop()
            continue

        if kind in ("produce_event_choice", "option_choice"):
            selected_box = (detail.get("selected") or {}).get("box")
            route = kind == "produce_event_choice"
            candidates = []
            for candidate in detail.get("candidates") or []:
                if not isinstance(candidate, dict):
                    continue
                for name, box in candidate.items():
                    if name == "SP":
                        continue
                    label = route_name(str(name), bool(candidate.get("SP")), course=route)
                    chosen = (selected_box is not None and box == selected_box) if route else label in subjects
                    candidates.append(ReplayChoice(label, chosen))
            label = "可选项（截图复核）" if detail.get("candidate_source") == "saved_frame_reanalysis" else "可选项"
            facts.append((label, candidates or "未记录"))
            before = observation_changes(detail.get("observations") or {}, last_values)
            if before:
                facts.append(("选择前读数", before))
        if (following or row)["detail"].get("framework_action_succeeded") is False:
            facts.append(("执行", "点击或执行失败"))
        battle = battle_key = None
        event = ReplayEvent(category, title, event_time, facts, pictures, subjects=subjects)
        if in_shop:
            shop.operations.append(event)
        else:
            finish_shop()
            events.append(event)
        if adjustment_identity is not None:
            last_adjustment = (adjustment_identity, event)
    finish_shop()
    if settlement := settlement_event(settlement_rows, final):
        events.append(settlement)
    return events


def picture_template(pictures, output):
    """只输出截图地址和缺失状态，浏览器展开事件后才请求图片。"""
    figures = []
    shown = set()
    for caption, evidence in pictures:
        raw_path = evidence.get("frame_path")
        if not raw_path:
            if evidence.get("frame_missing_reason"):
                figures.append(f'<figure><p class="image-status">截图未能保存。</p><figcaption>{escape(caption)}</figcaption></figure>')
            continue
        path = Path(raw_path)
        identity = evidence.get("frame_sha256") or path
        if identity in shown:
            continue
        shown.add(identity)
        root = Path(evidence["events_path"]).parent.resolve() if evidence.get("events_path") else None
        if (root is not None and not path.resolve().is_relative_to(root)) or not path.is_file():
            figures.append(f'<figure><p class="image-status">截图不可用，文件可能已被框架清理。</p>'
                           f'<figcaption>{escape(caption)}</figcaption></figure>')
            continue
        try:
            url = quote(Path(os.path.relpath(path, output)).as_posix(), safe="/")
        except ValueError:
            url = path.resolve().as_uri()
        url = escape(url, quote=True)
        figures.append(f'<figure><a hidden href="{url}" target="_blank" rel="noopener" title="查看原图">'
                       f'<img alt="{escape(caption)}" data-src="{url}" decoding="async"></a>'
                       f'<p class="image-status" role="status">正在加载截图…</p><figcaption>{escape(caption)}</figcaption></figure>')
    return f'<template class="shot-source">{"".join(figures)}</template>' if figures else ""


def render_replay(report):
    """按流程生成离线 HTML，保留结构化报告作为完整记录。"""
    session = report["session"]
    bindings = session["bindings"]
    configured = bindings.get("configured_idol") or {}
    zone = datetime.fromisoformat(session.get("local_started_at") or session["started_at"]).tzinfo
    output = Path(report["report_path"]).parent

    def clock(value):
        return local_clock(value, zone)

    def time_tag(value):
        return f'<time datetime="{escape(value or "")}">{clock(value)}</time>'

    def render_title(event):
        title = escape(event.title)
        if not event.subjects:
            return title
        values = '<span class="title-arrow"> → </span>'.join(f'<span class="game-value">{escape(name)}</span>' for name in event.subjects)
        return (f'<span class="summary-verb">{title}</span>{values}') if title else values

    def render_facts(facts):
        if not facts:
            return ""
        blocks = []
        for label, value in facts:
            if isinstance(value, dict):
                cells = "".join(f'<div><dt>{escape(name)}</dt><dd>{escape(reading)}</dd></div>' for name, reading in value.items())
                body = f'<dl class="fact-readings">{cells}</dl>'
            elif isinstance(value, list):
                choices = "".join(f'<li{" class=chosen" if choice.selected else ""}>{escape(choice.label)}'
                                  f'{"<small>已选</small>" if choice.selected else ""}</li>' for choice in value)
                body = f'<ul class="fact-choices">{choices}</ul>'
            else:
                body = f"<p>{escape(value)}</p>"
            blocks.append(f'<section class="fact"><h3>{escape(label)}</h3>{body}</section>')
        return f'<div class="facts">{"".join(blocks)}</div>'

    def render_operation(operation, event_id, *, show_category=False):
        shots = picture_template(operation.pictures, output)
        label = f'<span class="operation-kind">{escape(operation.category)}</span>' if show_category else ""
        return (f'<details class="operation" name="{event_id}-operations"><summary class="operation-summary">'
                f'<span class="summary-copy">{label}<strong>{render_title(operation)}</strong></span>{time_tag(operation.time)}</summary>'
                f'{render_facts(operation.facts)}{shots}</details>')

    def render_event(event, event_id):
        count = f'<small>{len(event.operations)} 次操作</small>' if event.operations else ""
        summary = (f'<span class="summary-copy"><span class="category">{escape(event.category)}</span>'
                   f'<strong class="event-title">{render_title(event)}</strong>{count}</span>{time_tag(event.time)}')
        shots = picture_template(event.pictures, output)
        if not (event.operations or event.facts or event.input_steps or shots):
            return f'<li><div class="event" id="{event_id}" tabindex="-1"><div class="event-summary">{summary}</div></div></li>'
        operations = "".join(render_operation(operation, event_id, show_category=event.category == "商店") for operation in event.operations)
        inputs = ""
        if event.input_steps:
            steps = "".join(f"<li>{escape(step)}</li>" for step in event.input_steps)
            inputs = f'<details class="shop-inputs"><summary>操作明细</summary><ol>{steps}</ol></details>'
        return (f'<li><details class="event" id="{event_id}" name="replay-events"><summary class="event-summary">{summary}</summary>'
                f'<div class="event-body">{operations}{render_facts(event.facts)}{inputs}</div>{shots}</details></li>')

    events = project_events(report)
    steps = []
    rows = []
    for number, event in enumerate(events, 1):
        event_id = f"event-{number}"
        label = escape(event.category)
        title = escape(" ".join(filter(None, (event.title, " → ".join(event.subjects)))))
        icon = FLOW_ICONS.get(event.category, '<path d="M5 5h14v14H5zM9 12h6"/>')
        steps.append(f'<li><a href="#{event_id}" aria-label="{label}，{title}，{clock(event.time)}">'
                     f'<svg viewBox="0 0 24 24" class="step-icon" aria-hidden="true">{icon}</svg>'
                     f'<span class="step-dot" aria-hidden="true"></span><span>{label}</span></a></li>')
        rows.append(render_event(event, event_id))
    overview = ('<nav class="overview" aria-label="流程总览"><div class="section-heading">'
                '<h2>流程总览</h2><div class="flow-controls" hidden>'
                '<button type="button" class="flow-prev" aria-controls="flow-window" aria-label="向左滚动流程"></button>'
                '<button type="button" class="flow-next" aria-controls="flow-window" aria-label="向右滚动流程"></button>'
                '</div></div><div class="flow-window" id="flow-window" tabindex="0" aria-label="横向滚动流程节点"><ol id="flow-steps" class="flow">'
                + "".join(steps) + '</ol></div></nav>') if steps else ""

    difficulty = bindings.get("difficulty") or "未知"
    if isinstance(difficulty, list):
        difficulty = "/".join(difficulty)
    status = "失败终止" if session.get("failure_stop_observed") else "已结束" if session["status"] == "ended" else "未完成"
    context = " · ".join(str(value) for value in (configured.get("idol_name"), configured.get("song_name")) if value)
    scenario = bindings.get("scenario") or "未知"
    final = report["final_settlement"]

    def metric(key):
        value = (final.get(key) or {}).get("value")
        return escape(f"{value:,}" if isinstance(value, int) else display(value))

    audition = report.get("final_audition")
    audition_summary = ""
    if audition:
        outcome = "不合格" if audition["outcome"] == "failed" else "合格"
        score = escape(display(audition["score"]["value"]))
        place = escape(display(audition["place"]["value"]))
        audition_summary = f'<p class="audition">最终试镜 {outcome} · {score} Pt · 名次 {place}</p>'
    warnings = []
    if not report.get("settlement_fields_complete"):
        warnings.append("部分结算数值未识别。")
    issues = (report.get("integrity") or {}).get("issues") or []
    if report.get("gaps") or any(issue.get("severity") == "error" and issue.get("code") not in {"frame_integrity", "missing_frame"} for issue in issues):
        warnings.append("部分过程或识别结果缺失，报告展示已记录的内容。")
    notes = "".join(f"<p>{escape(warning)}</p>" for warning in warnings)
    template = Template(Path(__file__).with_name("replay.html").read_text(encoding="utf-8"))
    html = template.substitute(context=escape(context), session=escape(f"{scenario} / {difficulty} · {status}"),
                               rank=metric("rank"), score=metric("score"), vo=metric("Vo"), da=metric("Da"), vi=metric("Vi"),
                               audition=audition_summary, overview=overview,
                               events="".join(rows) or '<li class="empty">本局没有可展示的事件记录。</li>', notes=notes)
    return html.splitlines(keepends=False)
