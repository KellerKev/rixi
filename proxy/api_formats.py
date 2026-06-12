# api_formats.py

import time
import uuid
from typing import Any, Dict, Optional, Callable


def to_internal(request_data: Dict[str, Any], api_format: str,
                map_model: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Convert API format to internal format"""
    def _map(requested_model):
        if map_model is None:
            return requested_model, True
        return map_model(requested_model)

    if api_format == "openai":
        messages = request_data.get("messages", [])
        if messages:
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    prompt_parts.append(f"System: {content}")
                elif role == "user":
                    prompt_parts.append(f"User: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"Assistant: {content}")

            prompt = "\n".join(prompt_parts)
            if not prompt.endswith("Assistant:"):
                prompt += "\nAssistant:"
        else:
            prompt = "User: Hello\nAssistant:"

        requested_model = request_data.get("model", "gpt-3.5-turbo")
        mapped_model, is_available = _map(requested_model)

        return {
            "command": "generate",
            "prompt": prompt,
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            "temperature": request_data.get("temperature", 0.7),
            "max_tokens": request_data.get("max_tokens"),
            "stream": request_data.get("stream", False)
        }

    elif api_format == "openai_responses":
        # OpenAI Responses API format
        requested_model = request_data.get("model", "gpt-3.5-turbo")
        mapped_model, is_available = _map(requested_model)

        # Handle input field
        input_text = request_data.get("input", "Hello")
        prompt = f"User: {input_text}\nAssistant:"

        return {
            "command": "generate",
            "prompt": prompt,
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            "temperature": request_data.get("temperature", 0.7),
            "max_tokens": request_data.get("max_tokens"),
            "stream": request_data.get("stream", False),
            "previous_response_id": request_data.get("previous_response_id"),
            "tools": request_data.get("tools", [])
        }

    elif api_format == "anthropic":
        messages = request_data.get("messages", [])
        if messages:
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "user":
                    prompt_parts.append(f"Human: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"Assistant: {content}")

            prompt = "\n".join(prompt_parts)
            if not prompt.endswith("Assistant:"):
                prompt += "\nAssistant:"
        else:
            prompt = "Human: Hello\nAssistant:"

        requested_model = request_data.get("model", "claude-3-sonnet-20240229")
        mapped_model, is_available = _map(requested_model)

        return {
            "command": "generate",
            "prompt": prompt,
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            "temperature": request_data.get("temperature", 0.7),
            "max_tokens": request_data.get("max_tokens", 1000),
            "stream": request_data.get("stream", False)
        }

    elif api_format == "ollama_generate":
        # Ollama Generate API format
        requested_model = request_data.get("model", "llama2")
        mapped_model, is_available = _map(requested_model)

        prompt = request_data.get("prompt", "Hello")
        system_msg = request_data.get("system")

        # Add system message if present
        if system_msg:
            prompt = f"System: {system_msg}\nUser: {prompt}\nAssistant:"
        else:
            prompt = f"User: {prompt}\nAssistant:"

        return {
            "command": "generate",
            "prompt": prompt,
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            "suffix": request_data.get("suffix"),
            "system": system_msg,
            "template": request_data.get("template"),
            "options": request_data.get("options", {}),
            "stream": request_data.get("stream", True),
            "raw": request_data.get("raw", False),
            "keep_alive": request_data.get("keep_alive", "5m")
        }

    elif api_format == "ollama_chat":
        # Ollama Chat API format
        messages = request_data.get("messages", [])
        requested_model = request_data.get("model", "llama2")
        mapped_model, is_available = _map(requested_model)

        if messages:
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    prompt_parts.append(f"System: {content}")
                elif role == "user":
                    prompt_parts.append(f"User: {content}")
                elif role == "assistant":
                    prompt_parts.append(f"Assistant: {content}")

            prompt = "\n".join(prompt_parts)
            if not prompt.endswith("Assistant:"):
                prompt += "\nAssistant:"
        else:
            prompt = "User: Hello\nAssistant:"

        return {
            "command": "generate",
            "prompt": prompt,
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            "options": request_data.get("options", {}),
            "stream": request_data.get("stream", True),
            "tools": request_data.get("tools", []),
            "keep_alive": request_data.get("keep_alive", "5m")
        }

    else:  # generic/passthrough
        requested_model = request_data.get("model", "default")
        mapped_model, is_available = _map(requested_model)

        return {
            "command": "generate",
            "prompt": request_data.get("prompt", "Hello"),
            "model": mapped_model,
            "model_available": is_available,
            "original_model": requested_model,
            **(request_data.get("parameters", {}))
        }


def from_internal(response_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
    """Convert internal response to API format"""
    content = response_data.get("response", "")

    if api_format == "openai":
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response_data.get("original_model", "gpt-3.5-turbo"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }

    elif api_format == "openai_responses":
        # OpenAI Responses API format
        return {
            "id": f"resp_{uuid.uuid4().hex[:24]}",
            "object": "response",
            "created": int(time.time()),
            "model": response_data.get("original_model", "gpt-3.5-turbo"),
            "status": "completed",
            "output": [{"type": "text", "text": content}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }

    elif api_format == "anthropic":
        return {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": content}],
            "model": response_data.get("original_model", "claude-3-sonnet"),
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0
            }
        }

    elif api_format == "ollama_generate":
        return {
            "model": response_data.get("original_model", "llama2"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
            "response": content,
            "done": True,
            "done_reason": "stop",
            "context": [],
            "total_duration": 0,
            "load_duration": 0,
            "prompt_eval_count": 0,
            "prompt_eval_duration": 0,
            "eval_count": 0,
            "eval_duration": 0
        }

    elif api_format == "ollama_chat":
        return {
            "model": response_data.get("original_model", "llama2"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
            "message": {
                "role": "assistant",
                "content": content
            },
            "done": True,
            "done_reason": "stop",
            "total_duration": 0,
            "load_duration": 0,
            "prompt_eval_count": 0,
            "prompt_eval_duration": 0,
            "eval_count": 0,
            "eval_duration": 0
        }

    else:  # generic
        return {
            "response": content,
            "model": response_data.get("original_model", "default"),
            "timestamp": time.time()
        }
