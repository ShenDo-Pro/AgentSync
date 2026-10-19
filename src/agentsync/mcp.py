"""Small stdio MCP bridge for Trae and CodeBuddy (newline JSON-RPC)."""

import json
import sqlite3
import sys

from .identity import resolve_project
from .messages import changes, publish
from .storage import connect, init_local, select_handoff


def tool_list() -> list[dict]:
    common = {"path": {"type": "string", "description": "Absolute local Git project path"}}
    definitions = [
        ("agentsync_messages", "Read new shared messages. Save next_cursor and pass it as after on the next call.",
         {"after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, []),
        ("agentsync_handoff", "Read the latest shared task context for the current branch.",
         {"task_id": {"type": "string"}}, []),
        ("agentsync_publish", "Share a user message or assistant reply with other IDEs. Use stable task/message IDs on retries.",
         {"source": {"type": "string", "enum": ["trae", "codebuddy", "codex"]},
          "task_id": {"type": "string"}, "message_id": {"type": "string"},
          "role": {"type": "string", "enum": ["user", "assistant"]},
          "content": {"type": "string"}, "timestamp": {"type": "string"},
          "sequence": {"type": "integer", "minimum": 0}},
         ["source", "task_id", "message_id", "role", "content"]),
    ]
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": {**common, **properties},
                             "required": ["path", *required], "additionalProperties": False}}
            for name, description, properties, required in definitions]


def call(db_path: str, name: str, args: dict) -> dict:
    if name == "agentsync_publish":
        return publish(db_path, **args)
    project = resolve_project(args["path"])
    if not project:
        raise ValueError("Not a Git project")
    with connect(db_path) as db:
        init_local(db)
        if name == "agentsync_messages":
            return changes(db, project.project_id, args.get("after", 0), args.get("limit", 100))
        if name == "agentsync_handoff":
            return select_handoff(db, project.project_id, project.branch, args.get("task_id", ""))
    raise ValueError("Unknown tool: " + name)


def serve(db_path: str) -> None:
    source = getattr(sys.stdin, "buffer", sys.stdin)
    for line in source:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ValueError("Invalid JSON-RPC request")
            if "id" not in request:
                continue
            method = request["method"]
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "agentsync", "version": "0.2.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                try:
                    params = request["params"]
                    value = call(db_path, params["name"], params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
                    result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            else:
                response = {"jsonrpc": "2.0", "id": request["id"],
                            "error": {"code": -32601, "message": "Method not found"}}
                print(json.dumps(response), flush=True)
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except (ValueError, TypeError, KeyError) as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32700 if isinstance(error, json.JSONDecodeError) else -32600,
                                  "message": str(error)}}
        print(json.dumps(response, ensure_ascii=True), flush=True)
