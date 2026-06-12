# test_clean_architecture.py - Tests for the configurable MCP agent
import asyncio
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
mcp_agent = pytest.importorskip("mcp_agent")

from mcp_agent import ConfigurableMCPAgent, create_agent_from_config
from mcp_manager import load_server_configs_from_dict

AGENT_DIR = Path(__file__).resolve().parent.parent


class MockChannel:
    server_url = "mock://test"
    task_id = "test-task"


@pytest.fixture
def agent():
    config = {
        "generation": {
            "default": {
                "prompt_template": "Write about {topic}",
                "post_processors": [{"type": "truncate", "max_length": 50}],
            }
        }
    }
    return ConfigurableMCPAgent(MockChannel(), config)


def test_agent_creation(agent):
    assert agent.config["generation"]["default"]["prompt_template"] == "Write about {topic}"
    assert agent.mcp_manager is not None
    assert agent._mcp_started is False


def test_create_agent_from_config_factory():
    agent = create_agent_from_config(MockChannel(), {"agent": {"name": "X"}})
    assert isinstance(agent, ConfigurableMCPAgent)
    assert agent.config["agent"]["name"] == "X"


def test_use_tool_requires_initialization(agent):
    with pytest.raises(RuntimeError):
        asyncio.run(agent.use_tool("read_file", path="x.txt"))


def test_has_tool_false_before_initialization(agent):
    assert asyncio.run(agent.has_tool("read_file")) is False


def test_get_available_tools_empty_before_initialization(agent):
    assert asyncio.run(agent.get_available_tools()) == {}


def test_build_prompt_substitutes_context(agent):
    generation_config = agent.config["generation"]["default"]
    prompt = agent._build_prompt_from_config({"topic": "nature"}, generation_config)
    assert "Write about nature" in prompt


def test_build_prompt_handles_missing_context_key(agent):
    generation_config = {"prompt_template": "About {missing_key}"}
    prompt = agent._build_prompt_from_config({"topic": "nature"}, generation_config)
    assert "{missing_key}" in prompt


def test_build_prompt_includes_constraints_and_instructions(agent):
    generation_config = {
        "prompt_template": "{topic}",
        "format_instructions": "Be brief.",
        "constraints": ["Three lines only"],
    }
    prompt = agent._build_prompt_from_config({"topic": "rain"}, generation_config)
    assert "Be brief." in prompt
    assert "- Three lines only" in prompt
    assert "rain" in prompt


def test_process_response_truncate(agent):
    config = {"post_processors": [{"type": "truncate", "max_length": 10}]}
    result = agent._process_response_from_config("a" * 100, config)
    assert len(result) <= 10


def test_process_response_extract_lines(agent):
    config = {"post_processors": [{"type": "extract_lines", "max_lines": 2}]}
    result = agent._process_response_from_config("one\ntwo\nthree\nfour", config)
    assert result == "one\ntwo"


def test_process_response_filter_patterns(agent):
    config = {"post_processors": [{"type": "filter_patterns", "patterns": ["example"]}]}
    result = agent._process_response_from_config("keep this\nExample: drop this", config)
    assert result == "keep this"


def test_resolve_params_from_context(agent):
    params = {"query": "${topic}", "static": "fixed"}
    resolved = agent._resolve_params_from_context(params, {"topic": "rivers"}, {})
    assert resolved == {"query": "rivers", "static": "fixed"}


def test_resolve_params_from_step_results(agent):
    params = {"content": "${research}"}
    resolved = agent._resolve_params_from_context(
        params, {}, {"research": {"result": "findings"}}
    )
    assert resolved == {"content": "findings"}


def test_agent_config_yaml_loads_and_produces_server_configs():
    config_path = AGENT_DIR / "agent_config.yaml"
    with open(config_path) as f:
        documents = list(yaml.safe_load_all(f))
    assert documents, "agent_config.yaml should contain at least one document"

    main_config = documents[0]
    assert "mcp" in main_config
    server_configs = load_server_configs_from_dict(main_config["mcp"])
    assert len(server_configs) == len(main_config["mcp"]["servers"])
    names = {c.name for c in server_configs}
    assert "filesystem" in names

    assert "workflows" in main_config
    for workflow in main_config["workflows"].values():
        assert "steps" in workflow
