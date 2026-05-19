"""Opt-in OpenAI decision layer for produce automation."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from utils import logger

DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_OUTPUT_TOKENS = 1024
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
LLM_NODE = "ProduceLLMDecision"

SYSTEM_PROMPT = """You are the opt-in decision layer for MaaGakumasu automatic produce mode in Gakuen Idolmaster.

Your job is to choose exactly one candidate_id from the current screen candidates. Candidate ids are opaque option handles; coordinates and automation node names are internal and are not part of the decision. Never invent coordinates and never invent ids. If the state is too ambiguous, choose the candidate that best preserves safety and progress, or set should_fallback=true.

The in-game "recommended" marker is intentionally hidden from you because it is noisy. Do not infer or request it. Judge choices from game mechanics, current state, card effects, plan synergy, and risk.

Core produce resources:
- Stamina/health is the main safety resource. Cards and some actions can consume it. If health is low, prefer recovery, low-cost cards, Genki protection, or a safe skip over high-cost scoring.
- Genki acts as a shield for stamina loss. When a card would reduce health, Genki is consumed first. Genki by itself does not score, but some Logic cards score from stored Genki.
- Vo, Da, and Vi are produce parameters. Attribute lessons raise the matching parameter. SP lessons are high value, but avoid pushing an attribute that is already near its current cap when another target attribute still needs growth.
- Turns are limited. Early turns favor setup, resource gain, draw, hand fixing, and long-duration buffs. Late turns favor immediate scoring, parameter gain, and consuming stored resources before the lesson/exam ends.

Plan and buff mechanics:
- Sense mainly uses Concentration and Good Condition/Kouchou. Concentration adds a flat bonus to score-gaining cards and becomes better the more score cards you still expect to play. Good Condition multiplies score by about 1.5 while active and also multiplies Concentration's added value. Prefer building/maintaining these before large score cards; do not spend late turns on setup that cannot pay off.
- Excellent Condition/Zekkouchou scales with the remaining Good Condition duration. It is strongest when Good Condition has enough turns and when there are strong score cards to play soon.
- Logic has two common routes. Good Impression/Ko-insho adds its stored value as parameter gain at turn end, then decreases over time, so it wants consistent stacking from early turns and reward picks that keep that route dense. Motivation/Yaruki increases future Genki gain, so it wants early Yaruki plus Genki generation, then cards that convert high Genki into score/parameter near the end.
- Do not mix Logic routes casually. If the current deck/state is clearly Good Impression, prefer more Good Impression and cards that exploit it. If it is clearly Yaruki/Genki, prefer Yaruki, Genki, and Genki payoff cards. Off-route cards need a strong immediate reason.
- Anomaly uses stance management: neutral, Conserve/Onzon, Aggressive/Tsuyoki, and Full Power/Zenryoku. Conserve lowers score and stamina cost, often preparing transition bonuses. Aggressive raises score but makes stamina cost riskier. Full Power is a burst stance after enough Full Power value is built; it is short-lived and cannot normally be switched away during the burst. Heat/Netsui is a one-turn flat score bonus, similar to temporary Concentration. Prefer stance cards when they set up an imminent payoff; prefer burst scoring during Aggressive or Full Power.

Card play policy:
- Only choose playable candidates unless skip_round is clearly better or all cards are unusable. Avoid candidates marked useless or disabled unless the state proves that label wrong.
- Prefer cards whose cost, conditions, and effects fit the current plan and turn timing. Setup/buff/draw/resource cards are stronger early; score/payoff/exhaust cards are stronger late.
- Consider one-time/limited-use cards carefully. Use them when their payoff is timely; avoid wasting them into caps, wrong stance, low buffs, or insufficient Genki.
- Unknown cards are allowed choices only when known candidates are worse, unsafe, or unavailable. If the only safe action is to pass, choose skip_round when offered.

Reward and event policy:
- Reward choices should improve deck consistency. Prefer cards that match the idol's plan route, fill missing setup/payoff, have manageable cost, and avoid bloating the deck with off-route or redundant cards. Skipping is acceptable when all rewards harm consistency.
- For regular produce events, protect health first, then prefer SP or target-attribute lessons that advance uncapped Vo/Da/Vi goals, then resource actions such as activity/work/shop/guide when health and points make them useful.
- For NIA, first and second target attributes are important. Prefer the first target until near cap, then the second target, while still taking valuable SP lessons and recovery when needed.
- Mirror choices should pick the highest reachable threshold that does not exceed the current vote count; choose none when no threshold is reachable.

Return only structured JSON. The dynamic current state is appended after this static prompt to improve prompt-cache reuse."""

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "candidate_id": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "should_fallback": {"type": "boolean"},
    },
    "required": ["candidate_id", "confidence", "reason", "should_fallback"],
}


def is_llm_enabled(context: Any) -> bool:
    try:
        return bool(context.get_node_data(LLM_NODE).get("enabled", False))
    except Exception as exc:  # pragma: no cover - defensive around Maa runtime.
        logger.debug(f"LLM decision node unavailable: {exc}")
        return False


def _find_key_in_value(value: Any) -> str:
    if isinstance(value, dict):
        raw_key = value.get("openai_api_key") or value.get("api_key")
        if isinstance(raw_key, str) and raw_key.strip():
            return raw_key.strip()
        for item in value.values():
            found = _find_key_in_value(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key_in_value(item)
            if found:
                return found
    return ""


def _api_key_from_saved_config() -> str:
    roots = [
        Path.cwd() / "config" / "instances",
        Path(__file__).resolve().parents[3] / "config" / "instances",
    ]
    seen: set[Path] = set()
    files: List[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    for path in sorted(files, key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            found = _find_key_in_value(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if found:
            return found
    return ""


def _api_key_from_context(context: Any) -> str:
    try:
        node_data = context.get_node_data(LLM_NODE) or {}
    except Exception as exc:  # pragma: no cover - defensive around Maa runtime.
        logger.debug(f"LLM decision API key unavailable from node data: {exc}")
        return ""
    api_key = node_data.get("api_key", "")
    if not api_key:
        api_key = node_data.get("custom_action_param", {}).get("api_key", "")
    if not api_key:
        api_key = node_data.get("action", {}).get("param", {}).get("api_key", "")
    if not api_key:
        api_key = node_data.get("recognition", {}).get("param", {}).get("api_key", "")
    if not isinstance(api_key, str):
        return ""
    return api_key.strip() or _api_key_from_saved_config()


def _timeout() -> float:
    raw_value = os.environ.get("MAAGAKUMASU_LLM_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SEC))
    try:
        return max(1.0, min(float(raw_value), float(DEFAULT_TIMEOUT_SEC)))
    except ValueError:
        return float(DEFAULT_TIMEOUT_SEC)


def _model() -> str:
    return os.environ.get("MAAGAKUMASU_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _api_url() -> str:
    return os.environ.get("MAAGAKUMASU_OPENAI_RESPONSES_URL", OPENAI_RESPONSES_URL).strip() or OPENAI_RESPONSES_URL


def _max_output_tokens() -> int:
    raw_value = os.environ.get("MAAGAKUMASU_LLM_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))
    try:
        return max(256, int(raw_value))
    except ValueError:
        return DEFAULT_MAX_OUTPUT_TOKENS


HIDDEN_CANDIDATE_FIELDS = {"box", "click_point", "recommended", "rule_node"}
RECOMMENDATION_MARKER_VALUES = {"recommended", "event_recommended", "recommend", "event_recommend"}


def _candidate_ids(candidates: Iterable[Dict[str, Any]]) -> set:
    return {str(candidate.get("candidate_id")) for candidate in candidates if candidate.get("candidate_id")}


def _strip_internal_candidate_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_internal_candidate_data(item) for key, item in value.items() if key not in HIDDEN_CANDIDATE_FIELDS}
    if isinstance(value, list):
        return [_strip_internal_candidate_data(item) for item in value]
    return value


def _llm_candidates(candidates: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    llm_candidates: List[Dict[str, Any]] = []
    llm_id_to_source_id: Dict[str, str] = {}
    for index, candidate in enumerate(candidates, start=1):
        source_id = str(candidate.get("candidate_id", ""))
        if not source_id:
            continue
        llm_id = f"candidate_{index}"
        llm_id_to_source_id[llm_id] = source_id
        sanitized = _strip_internal_candidate_data(candidate)
        sanitized["candidate_id"] = llm_id
        name = sanitized.get("name")
        if isinstance(name, str) and name.lower() in RECOMMENDATION_MARKER_VALUES:
            sanitized["name"] = f"reward_option_{index}"
        llm_candidates.append(sanitized)
    return llm_candidates, llm_id_to_source_id


def _build_payload(screen: str, state: Dict[str, Any], candidates: List[Dict[str, Any]], fallback: str) -> Dict[str, Any]:
    dynamic_state = {
        "screen": screen,
        "fallback_policy": fallback,
        "state": _strip_internal_candidate_data(state),
        "candidates": candidates,
    }
    return {
        "model": _model(),
        "instructions": SYSTEM_PROMPT,
        "input": json.dumps(dynamic_state, ensure_ascii=False, separators=(",", ":")),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "maagakumasu_produce_decision",
                "schema": DECISION_SCHEMA,
                "strict": True,
            }
        },
        "prompt_cache_key": "maagakumasu-produce-v1",
        "prompt_cache_retention": "24h",
        "max_output_tokens": _max_output_tokens(),
    }


def _extract_output_text(response: Dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    output = response.get("output", [])
    if not isinstance(output, list):
        return ""
    chunks: List[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for chunk in content:
            if not isinstance(chunk, dict):
                continue
            text = chunk.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def parse_decision_response(response: Dict[str, Any], valid_ids: set) -> Optional[Dict[str, Any]]:
    text = _extract_output_text(response).strip()
    if not text:
        return None
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM decision response was not valid JSON")
        return None
    if not isinstance(decision, dict):
        return None
    if decision.get("should_fallback"):
        return None
    candidate_id = str(decision.get("candidate_id", ""))
    if candidate_id not in valid_ids:
        logger.warning(f"LLM returned invalid candidate_id: {candidate_id}")
        return None
    return decision


def _post_payload(payload: Dict[str, Any], api_key: str, timeout: float, opener: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _api_url(),
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    with open_fn(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def request_llm_decision(
    screen: str,
    state: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    fallback: str,
    *,
    api_key: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    sanitized_candidates, llm_id_to_source_id = _llm_candidates(candidates)
    valid_ids = _candidate_ids(sanitized_candidates)
    api_key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        logger.info("LLM decision skipped: OpenAI API key is not set")
        return None
    payload = _build_payload(screen, state, sanitized_candidates, fallback)
    try:
        response = _post_payload(payload, api_key, _timeout(), opener=opener)
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and ("prompt_cache_key" in payload or "prompt_cache_retention" in payload):
            logger.warning("LLM request rejected cache fields; retrying once without cache hints")
            payload.pop("prompt_cache_key", None)
            payload.pop("prompt_cache_retention", None)
            try:
                response = _post_payload(payload, api_key, _timeout(), opener=opener)
            except (OSError, urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as retry_exc:
                logger.warning(f"LLM decision failed after retry: {retry_exc}")
                return None
        else:
            logger.warning(f"LLM decision HTTP failure: {exc}")
            return None
    except (OSError, urllib.error.URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
        logger.warning(f"LLM decision failed: {exc}")
        return None
    decision = parse_decision_response(response, valid_ids)
    if decision:
        llm_candidate_id = str(decision.get("candidate_id", ""))
        decision["llm_candidate_id"] = llm_candidate_id
        decision["candidate_id"] = llm_id_to_source_id.get(llm_candidate_id, llm_candidate_id)
    if decision:
        logger.info(
            "LLM decision: {candidate_id} confidence={confidence:.2f} reason={reason}".format(
                candidate_id=decision.get("candidate_id"),
                confidence=float(decision.get("confidence", 0)),
                reason=decision.get("reason", ""),
            )
        )
    return decision


def choose_candidate(
    context: Any,
    screen: str,
    state: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    fallback: str,
) -> Optional[Dict[str, Any]]:
    if not is_llm_enabled(context):
        return None
    decision = request_llm_decision(screen, state, candidates, fallback, api_key=_api_key_from_context(context))
    if not decision:
        return None
    candidate_id = decision["candidate_id"]
    for candidate in candidates:
        if candidate.get("candidate_id") == candidate_id:
            candidate = dict(candidate)
            candidate["llm_confidence"] = decision.get("confidence")
            candidate["llm_reason"] = decision.get("reason")
            return candidate
    return None
