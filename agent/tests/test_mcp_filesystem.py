# test_mcp_filesystem.py - Tests for the filesystem MCP server
import asyncio
import pathlib

import pytest

from mcp_servers import FilesystemMCPServer, MCPFilesystemHandler


def test_ensure_safe_path_accepts_paths_inside_root(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    inside = tmp_path / "subdir" / "file.txt"
    assert server.ensure_safe_path(inside) == inside.resolve()


def test_ensure_safe_path_rejects_parent_traversal(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    with pytest.raises(PermissionError):
        server.ensure_safe_path(tmp_path / ".." / "outside.txt")


def test_ensure_safe_path_rejects_absolute_path_outside_root(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    with pytest.raises(PermissionError):
        server.ensure_safe_path(pathlib.Path("/etc/passwd"))


def test_read_file_rejects_traversal(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    result = asyncio.run(server.read_file("../outside.txt"))
    assert result["success"] is False
    assert "Access denied" in result["error"]


def test_write_file_rejects_absolute_path_outside_root(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    result = asyncio.run(server.write_file("/tmp/definitely_outside.txt", "nope"))
    assert result["success"] is False
    assert "Access denied" in result["error"]


def test_write_and_read_roundtrip(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    write_result = asyncio.run(server.write_file("notes/hello.txt", "hello mcp"))
    assert write_result["success"] is True
    assert (tmp_path / "notes" / "hello.txt").read_text() == "hello mcp"

    read_result = asyncio.run(server.read_file("notes/hello.txt"))
    assert read_result["success"] is True
    assert read_result["content"] == "hello mcp"


def test_read_file_missing_returns_error(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    result = asyncio.run(server.read_file("does_not_exist.txt"))
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_read_file_enforces_size_cap(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    server.MAX_READ_BYTES = 16
    (tmp_path / "big.txt").write_text("x" * 100)
    result = asyncio.run(server.read_file("big.txt"))
    assert result["success"] is False
    assert "too large" in result["error"].lower()


def test_list_directory(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    result = asyncio.run(server.list_directory("."))
    assert result["success"] is True
    names = {item["name"] for item in result["items"]}
    assert names == {"a.txt", "sub"}


def test_create_directory(tmp_path):
    server = FilesystemMCPServer(str(tmp_path))
    result = asyncio.run(server.create_directory("new/nested"))
    assert result["success"] is True
    assert (tmp_path / "new" / "nested").is_dir()


def test_handler_unknown_tool(tmp_path):
    handler = MCPFilesystemHandler(str(tmp_path))
    result = asyncio.run(handler.handle_tool_call("delete_everything", {}))
    assert result["success"] is False
    assert "Unknown tool" in result["error"]


def test_handler_routes_tool_calls(tmp_path):
    handler = MCPFilesystemHandler(str(tmp_path))
    result = asyncio.run(handler.handle_tool_call(
        "write_file", {"path": "via_handler.txt", "content": "ok"}
    ))
    assert result["success"] is True
    assert result["tool_name"] == "write_file"
    assert (tmp_path / "via_handler.txt").read_text() == "ok"
