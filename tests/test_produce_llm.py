import importlib.util
import json
import sys
from pathlib import Path
from socket import timeout as SocketTimeout
from urllib.error import HTTPError, URLError

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent"))
MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "custom" / "action" / "produce_llm.py"
SPEC = importlib.util.spec_from_file_location("produce_llm", MODULE_PATH)
produce_llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(produce_llm)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeContext:
    def __init__(self, node_data):
        self.node_data = node_data

    def get_node_data(self, node):
        assert node == produce_llm.LLM_NODE
        return self.node_data


def test_parse_decision_response_accepts_valid_candidate():
    response = {"output": [{"content": [{"text": '{"candidate_id":"event_1","confidence":0.9,"reason":"best","should_fallback":false}'}]}]}

    decision = produce_llm.parse_decision_response(response, {"event_1"})

    assert decision["candidate_id"] == "event_1"
    assert decision["confidence"] == 0.9


def test_parse_decision_response_rejects_invalid_candidate():
    response = {"output_text": '{"candidate_id":"made_up","confidence":0.9,"reason":"bad","should_fallback":false}'}

    assert produce_llm.parse_decision_response(response, {"event_1"}) is None


def test_parse_decision_response_should_fallback_true_returns_none():
    response = {"output_text": '{"candidate_id":"event_1","confidence":0.5,"reason":"fallback","should_fallback":true}'}

    assert produce_llm.parse_decision_response(response, {"event_1"}) is None


def test_parse_decision_response_malformed_json_returns_none():
    response = {"output_text": '{"candidate_id":"event_1","confidence":0.9,"reason":"bad json","should_fallback":false'}

    assert produce_llm.parse_decision_response(response, {"event_1"}) is None


def test_parse_decision_response_non_object_json_returns_none_for_list():
    response = {"output_text": '["event_1", "event_2"]'}

    assert produce_llm.parse_decision_response(response, {"event_1"}) is None


def test_parse_decision_response_non_object_json_returns_none_for_string():
    response = {"output_text": '"just a string, not an object"'}

    assert produce_llm.parse_decision_response(response, {"event_1"}) is None


def test_request_llm_decision_uses_structured_response(monkeypatch):
    captured = {}

    def fake_open(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse({"output_text": '{"candidate_id":"candidate_1","confidence":0.8,"reason":"safe","should_fallback":false}'})

    monkeypatch.setenv("MAAGAKUMASU_LLM_TIMEOUT_SEC", "30")

    decision = produce_llm.request_llm_decision(
        "reward_choice",
        {"score": {"Vo": 100}},
        [
            {
                "candidate_id": "reward_recommend",
                "name": "recommended",
                "box": [1, 2, 3, 4],
                "click_point": [2, 4],
                "rule_node": "ProduceChooseRecommend",
                "recommended": True,
                "playable": True,
            }
        ],
        "fallback",
        api_key="test-key",
        opener=fake_open,
    )

    assert decision["candidate_id"] == "reward_recommend"
    assert captured["body"]["model"] == produce_llm.DEFAULT_MODEL
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    assert captured["body"]["prompt_cache_key"] == "maagakumasu-produce-v1"

    dynamic_state = json.loads(captured["body"]["input"])
    llm_candidate = dynamic_state["candidates"][0]
    assert llm_candidate["candidate_id"] == "candidate_1"
    assert llm_candidate["name"] == "reward_option_1"
    assert "recommended" not in llm_candidate
    assert "box" not in llm_candidate
    assert "click_point" not in llm_candidate
    assert "rule_node" not in llm_candidate


def test_request_llm_decision_retries_on_http_error_and_strips_cache_keys(monkeypatch):
    calls = {"count": 0, "payloads": []}

    def fake_post_payload(payload, api_key, timeout, opener=None):
        calls["count"] += 1
        calls["payloads"].append(dict(payload))
        if calls["count"] == 1:
            raise HTTPError(produce_llm.OPENAI_RESPONSES_URL, 400, "Bad Request", hdrs=None, fp=None)
        return {"output_text": '{"candidate_id":"candidate_1","confidence":0.9,"reason":"ok","should_fallback":false}'}

    monkeypatch.setattr(produce_llm, "_post_payload", fake_post_payload)

    decision = produce_llm.request_llm_decision(
        "reward_choice",
        {},
        [{"candidate_id": "event_1"}],
        "fallback",
        api_key="test-key",
    )

    assert decision["candidate_id"] == "event_1"
    assert calls["count"] == 2
    first_payload, second_payload = calls["payloads"]
    assert "prompt_cache_key" in first_payload
    assert "prompt_cache_retention" in first_payload
    assert "prompt_cache_key" not in second_payload
    assert "prompt_cache_retention" not in second_payload


def test_request_llm_decision_returns_none_on_timeout(monkeypatch):
    def fake_post_payload(*args, **kwargs):
        raise SocketTimeout("timed out")

    monkeypatch.setattr(produce_llm, "_post_payload", fake_post_payload)

    assert produce_llm.request_llm_decision("reward_choice", {}, [{"candidate_id": "event_1"}], "fallback", api_key="test-key") is None


def test_request_llm_decision_returns_none_on_urlerror(monkeypatch):
    def fake_post_payload(*args, **kwargs):
        raise URLError("connection failed")

    monkeypatch.setattr(produce_llm, "_post_payload", fake_post_payload)

    assert produce_llm.request_llm_decision("reward_choice", {}, [{"candidate_id": "event_1"}], "fallback", api_key="test-key") is None


def test_choose_candidate_uses_gui_api_key(monkeypatch):
    captured = {}

    def fake_request(screen, state, candidates, fallback, *, api_key=None):
        captured["api_key"] = api_key
        return {"candidate_id": "event_1", "confidence": 0.75, "reason": "gui key"}

    monkeypatch.setattr(produce_llm, "request_llm_decision", fake_request)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    choice = produce_llm.choose_candidate(
        FakeContext({"enabled": True, "custom_action_param": {"api_key": "  gui-key  "}}),
        "event_hajime",
        {},
        [{"candidate_id": "event_1"}],
        "fallback",
    )

    assert choice["candidate_id"] == "event_1"
    assert captured["api_key"] == "gui-key"


def test_choose_candidate_still_accepts_legacy_top_level_gui_api_key(monkeypatch):
    captured = {}

    def fake_request(screen, state, candidates, fallback, *, api_key=None):
        captured["api_key"] = api_key
        return {"candidate_id": "event_1", "confidence": 0.75, "reason": "legacy key"}

    monkeypatch.setattr(produce_llm, "request_llm_decision", fake_request)

    choice = produce_llm.choose_candidate(
        FakeContext({"enabled": True, "api_key": "legacy-key"}),
        "event_hajime",
        {},
        [{"candidate_id": "event_1"}],
        "fallback",
    )

    assert choice["candidate_id"] == "event_1"
    assert captured["api_key"] == "legacy-key"


def test_choose_candidate_accepts_action_param_gui_api_key(monkeypatch):
    captured = {}

    def fake_request(screen, state, candidates, fallback, *, api_key=None):
        captured["api_key"] = api_key
        return {"candidate_id": "event_1", "confidence": 0.75, "reason": "action param key"}

    monkeypatch.setattr(produce_llm, "request_llm_decision", fake_request)

    choice = produce_llm.choose_candidate(
        FakeContext({"enabled": True, "action": {"param": {"api_key": "action-param-key"}}}),
        "event_hajime",
        {},
        [{"candidate_id": "event_1"}],
        "fallback",
    )

    assert choice["candidate_id"] == "event_1"
    assert captured["api_key"] == "action-param-key"


def test_api_key_from_saved_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "config" / "instances"
    config_dir.mkdir(parents=True)
    (config_dir / "instance.json").write_text(
        json.dumps({"tasks": [{"name": "OpenAI_API_Key输入", "input": {"openai_api_key": "saved-key"}}]}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert produce_llm._api_key_from_context(FakeContext({"enabled": True})) == "saved-key"


def test_choose_candidate_allows_env_api_key_fallback(monkeypatch):
    captured = {}

    def fake_request(screen, state, candidates, fallback, *, api_key=None):
        captured["api_key"] = api_key
        return {"candidate_id": "event_1", "confidence": 0.75, "reason": "env key"}

    monkeypatch.setattr(produce_llm, "request_llm_decision", fake_request)

    choice = produce_llm.choose_candidate(
        FakeContext({"enabled": True, "api_key": ""}),
        "event_hajime",
        {},
        [{"candidate_id": "event_1"}],
        "fallback",
    )

    assert choice["candidate_id"] == "event_1"
    assert captured["api_key"] == ""
