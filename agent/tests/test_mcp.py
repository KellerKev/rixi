# test_mcp.py - Tests for the MCP manager
import asyncio

import pytest

from mcp_manager import (
    MCPManager,
    MCPServerConfig,
    create_filesystem_server_config,
    create_web_search_server_config,
    load_server_configs_from_dict,
)


def test_create_filesystem_server_config():
    config = create_filesystem_server_config("workspace", "/data")
    assert config.name == "workspace"
    assert config.command == ["python", "-m", "mcp_servers", "filesystem", "/data"]
    assert "read_file" in config.tools
    assert config.mode == "real"


def test_create_web_search_server_config():
    config = create_web_search_server_config("search", api_key="secret")
    assert config.name == "search"
    assert config.env_vars == {"API_KEY": "secret"}
    assert "web_search" in config.tools


def test_server_config_from_dict():
    config = MCPServerConfig.from_dict({
        "name": "test_server",
        "command": ["python", "-m", "test"],
        "tools": ["test_tool"],
        "mode": "simulation",
    })
    assert config.name == "test_server"
    assert config.tools == ["test_tool"]
    assert config.mode == "simulation"


def test_load_server_configs_from_dict():
    configs = load_server_configs_from_dict({
        "servers": [
            {"name": "a", "command": ["x"], "tools": ["t1"]},
            {"name": "b", "command": ["y"]},
        ]
    })
    assert [c.name for c in configs] == ["a", "b"]


def test_load_server_configs_respects_prefer_real_flag():
    configs = load_server_configs_from_dict({
        "prefer_real_servers": False,
        "servers": [{"name": "a", "command": ["x"]}],
    })
    assert configs[0].mode == "simulation"


def test_call_tool_unknown_tool_raises():
    async def scenario():
        manager = MCPManager()
        await manager.start()
        try:
            with pytest.raises(ValueError):
                await manager.call_tool("nonexistent_tool", {})
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_filesystem_tool_roundtrip_via_manager(tmp_path):
    async def scenario():
        manager = MCPManager(prefer_real_servers=True)
        await manager.start()
        try:
            config = create_filesystem_server_config("test_fs", str(tmp_path))
            assert await manager.register_server(config)
            assert await manager.start_server("test_fs")

            write_result = await manager.call_tool(
                "write_file", {"path": "from_manager.txt", "content": "hello"}
            )
            assert write_result["success"] is True
            assert write_result["server_mode"] == "real"
            assert (tmp_path / "from_manager.txt").read_text() == "hello"

            read_result = await manager.call_tool(
                "read_file", {"path": "from_manager.txt"}
            )
            assert read_result["success"] is True
            assert read_result["content"] == "hello"
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_real_mode_errors_are_not_masked(tmp_path):
    async def scenario():
        manager = MCPManager(prefer_real_servers=True)
        await manager.start()
        try:
            config = create_filesystem_server_config("test_fs", str(tmp_path))
            await manager.register_server(config)
            await manager.start_server("test_fs")

            result = await manager.call_tool(
                "read_file", {"path": "../escape_attempt.txt"}
            )
            assert result["success"] is False
            assert result["server_mode"] == "real"
            assert "error" in result
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_real_mode_exception_propagates(tmp_path, monkeypatch):
    async def scenario():
        manager = MCPManager(prefer_real_servers=True)
        await manager.start()
        try:
            config = create_filesystem_server_config("test_fs", str(tmp_path))
            await manager.register_server(config)
            await manager.start_server("test_fs")

            async def boom(tool_name, params, instance):
                raise RuntimeError("backend exploded")

            monkeypatch.setattr(manager, "_call_real_filesystem_tool", boom)

            with pytest.raises(RuntimeError, match="backend exploded"):
                await manager.call_tool("read_file", {"path": "x.txt"})
        finally:
            await manager.stop()

    asyncio.run(scenario())


def test_simulation_mode_is_labelled(tmp_path):
    async def scenario():
        manager = MCPManager(prefer_real_servers=False)
        await manager.start()
        try:
            config = create_filesystem_server_config(
                "sim_fs", str(tmp_path), mode="simulation"
            )
            await manager.register_server(config)
            await manager.start_server("sim_fs")

            result = await manager.call_tool(
                "write_file", {"path": "sim.txt", "content": "x"}
            )
            assert result["server_mode"] == "simulated"
            assert not (tmp_path / "sim.txt").exists()
        finally:
            await manager.stop()

    asyncio.run(scenario())
