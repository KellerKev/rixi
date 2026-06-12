#!/usr/bin/env python3
"""
Universal Proxy Server - CLEAN VERSION
Provides OpenAI, Anthropic, Responses API, and generic HTTP API compatibility
for remote inference services via secure Pixi runner infrastructure.
"""

import asyncio
import argparse
import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List, AsyncGenerator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────── Models ────────────────────────────────────────

class OpenAIMessage(BaseModel):
    role: str
    content: str

class OpenAIChatRequest(BaseModel):
    model: str = "gpt-3.5-turbo"
    messages: List[OpenAIMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False

class OpenAIResponseRequest(BaseModel):
    model: str = "gpt-3.5-turbo"
    input: str  # The main input for the response
    previous_response_id: Optional[str] = None
    tools: Optional[List[Dict[str, Any]]] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False

class AnthropicMessage(BaseModel):
    role: str
    content: str

class AnthropicRequest(BaseModel):
    model: str = "claude-3-sonnet-20240229"
    messages: List[AnthropicMessage]
    max_tokens: int = 1000
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False

class OllamaGenerateRequest(BaseModel):
    model: str
    prompt: str
    suffix: Optional[str] = None
    images: Optional[List[str]] = None  # Base64 encoded images
    format: Optional[str] = None  # JSON schema for structured output
    options: Optional[Dict[str, Any]] = None  # Model parameters like temperature
    system: Optional[str] = None
    template: Optional[str] = None
    context: Optional[List[int]] = None
    stream: Optional[bool] = True
    raw: Optional[bool] = False
    keep_alive: Optional[str] = "5m"

class OllamaChatMessage(BaseModel):
    role: str  # system, user, assistant
    content: str
    images: Optional[List[str]] = None

class OllamaChatRequest(BaseModel):
    model: str
    messages: List[OllamaChatMessage]
    tools: Optional[List[Dict[str, Any]]] = None
    format: Optional[str] = None  # JSON schema for structured output
    options: Optional[Dict[str, Any]] = None
    stream: Optional[bool] = True
    keep_alive: Optional[str] = "5m"

class GenericRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None

class ProxyConfig(BaseModel):
    mode: str = "openai"  # openai, anthropic, generic
    inference_task_id: Optional[str] = None
    server_url: str = "http://localhost:9000"
    aes_key: Optional[bytes] = None
    auth_headers: Optional[Dict[str, str]] = None
    health_check_interval: int = 30
    request_timeout: int = 120
    enable_logging: bool = True

# ─────────────────────────── Remote Channel ────────────────────────────────

class RemoteChannel:
    """Handles communication with remote inference tasks"""
    
    def __init__(self, server_url: str, task_id: str, aes_key: Optional[bytes] = None, 
                 auth_headers: Optional[Dict[str, str]] = None):
        self.server_url = server_url.rstrip('/')
        self.task_id = task_id
        self.aes_key = aes_key
        self.auth_headers = auth_headers or {}
        self.NONCE_LEN = 12
        
    def _encrypt(self, data: str) -> bytes:
        """Encrypt data if AES key is available"""
        if not self.aes_key:
            return data.encode('utf-8')
        
        nonce = os.urandom(self.NONCE_LEN)
        return nonce + AESGCM(self.aes_key).encrypt(nonce, data.encode('utf-8'), None)
    
    def _decrypt(self, data: bytes) -> str:
        """Decrypt data if AES key is available"""
        if not self.aes_key:
            return data.decode('utf-8', errors='ignore')
        
        try:
            plaintext = AESGCM(self.aes_key).decrypt(data[:self.NONCE_LEN], data[self.NONCE_LEN:], None)
            return plaintext.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return data.decode('utf-8', errors='ignore')
    
    async def send_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send request to remote inference service"""
        import aiohttp
        
        request_id = str(uuid.uuid4())
        request_data["request_id"] = request_id
        
        # Prepare headers
        headers = self.auth_headers.copy()
        if self.aes_key:
            headers["Content-Type"] = "application/octet-stream"
        else:
            headers["Content-Type"] = "application/json"
        
        # Prepare payload
        json_data = json.dumps(request_data)
        if self.aes_key:
            payload = self._encrypt(json_data)
        else:
            payload = json_data.encode('utf-8')
        
        url = f"{self.server_url}/task/{self.task_id}/input"
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, data=payload, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise HTTPException(status_code=response.status, detail=f"Remote service error: {error_text}")
                    
                    result = await response.json()
                    return {"success": True, "data": result, "request_id": request_id}
                    
            except aiohttp.ClientError as e:
                logger.error(f"Request failed: {e}")
                raise HTTPException(status_code=500, detail=f"Remote service unavailable: {str(e)}")
    
    async def _poll_for_response(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Poll for response when streaming fails or for non-streaming requests"""
        import aiohttp
        
        url = f"{self.server_url}/task/{self.task_id}"
        headers = self.auth_headers.copy()
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Look for our response in recent output
                        for output in data.get("recent_output", []):
                            if output.get("type") == "output":
                                try:
                                    output_data = json.loads(output["content"])
                                    if output_data.get("request_id") == request_id:
                                        return output_data
                                except (json.JSONDecodeError, KeyError):
                                    continue
                return None
            except Exception as e:
                logger.error(f"Polling failed: {e}")
                return None
    
    async def health_check(self) -> bool:
        """Check if remote service is healthy"""
        try:
            import aiohttp
            url = f"{self.server_url}/task/{self.task_id}"
            headers = self.auth_headers.copy()
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("status") in ["running", "completed"]
                    return False
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

# ─────────────────────────── Proxy Server ──────────────────────────────────

class UniversalProxyServer:
    """Universal proxy server supporting multiple API formats"""
    
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.channel = None
        self.health_status = {"healthy": False, "last_check": 0}
        self.request_count = 0
        self.start_time = time.time()
        
        # Initialize channel if task ID provided
        if config.inference_task_id:
            self.channel = RemoteChannel(
                config.server_url,
                config.inference_task_id,
                config.aes_key,
                config.auth_headers
            )
        
        logger.info(f"Proxy server initialized in {config.mode} mode")
    
    async def start_health_monitor(self):
        """Start health monitoring (called from lifespan)"""
        if self.channel:
            asyncio.create_task(self._health_monitor())
    
    async def _health_monitor(self):
        """Background health monitoring"""
        while True:
            try:
                if self.channel:
                    is_healthy = await self.channel.health_check()
                    self.health_status = {
                        "healthy": is_healthy,
                        "last_check": time.time()
                    }
                    if not is_healthy:
                        logger.warning("Remote inference service is unhealthy")
                
                await asyncio.sleep(self.config.health_check_interval)
            except Exception as e:
                logger.error(f"Health monitor error: {e}")
                await asyncio.sleep(self.config.health_check_interval)
    
    def _convert_to_internal_format(self, request_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
        """Convert API request to internal format"""
        if api_format == "openai":
            messages = request_data.get("messages", [])
            if messages:
                # Combine messages into a single prompt
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
            else:
                prompt = "Hello"
            
            return {
                "command": "generate",
                "prompt": prompt,
                "model": request_data.get("model", "default"),
                "temperature": request_data.get("temperature", 0.7),
                "max_tokens": request_data.get("max_tokens"),
                "stream": request_data.get("stream", False)
            }
        
        elif api_format == "openai_responses":
            # Convert OpenAI Responses API format to internal
            return {
                "command": "generate",
                "prompt": request_data.get("input", "Hello"),
                "model": request_data.get("model", "gpt-3.5-turbo"),
                "temperature": request_data.get("temperature", 0.7),
                "max_tokens": request_data.get("max_tokens"),
                "stream": request_data.get("stream", False),
                "previous_response_id": request_data.get("previous_response_id"),
                "tools": request_data.get("tools", [])
            }
        
        elif api_format == "ollama_generate":
            # Convert Ollama Generate API format to internal
            return {
                "command": "generate",
                "prompt": request_data.get("prompt", "Hello"),
                "model": request_data.get("model", "llama2"),
                "suffix": request_data.get("suffix"),
                "system": request_data.get("system"),
                "template": request_data.get("template"),
                "options": request_data.get("options", {}),
                "stream": request_data.get("stream", True),
                "raw": request_data.get("raw", False),
                "keep_alive": request_data.get("keep_alive", "5m")
            }
        
        elif api_format == "ollama_chat":
            # Convert Ollama Chat API format to internal
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
            else:
                prompt = "Hello"
            
            return {
                "command": "generate",
                "prompt": prompt,
                "model": request_data.get("model", "llama2"),
                "options": request_data.get("options", {}),
                "stream": request_data.get("stream", True),
                "tools": request_data.get("tools", []),
                "keep_alive": request_data.get("keep_alive", "5m")
            }
        
        elif api_format == "ollama_generate":
            # Ollama Generate API format
            return {
                "model": response_data.get("model", "llama2"),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                "response": content,
                "done": True,
                "done_reason": "stop",
                "context": [],  # Would contain actual context in real implementation
                "total_duration": 0,
                "load_duration": 0,
                "prompt_eval_count": 0,
                "prompt_eval_duration": 0,
                "eval_count": 0,
                "eval_duration": 0
            }
        
        elif api_format == "ollama_chat":
            # Ollama Chat API format
            return {
                "model": response_data.get("model", "llama2"),
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
        
        elif api_format == "anthropic":
            messages = request_data.get("messages", [])
            if messages:
                # Convert Anthropic format
                prompt_parts = []
                for msg in messages:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role == "human":
                        prompt_parts.append(f"Human: {content}")
                    elif role == "assistant":
                        prompt_parts.append(f"Assistant: {content}")
                
                prompt = "\n".join(prompt_parts)
            else:
                prompt = "Hello"
            
            return {
                "command": "generate",
                "prompt": prompt,
                "model": request_data.get("model", "claude"),
                "temperature": request_data.get("temperature", 0.7),
                "max_tokens": request_data.get("max_tokens", 1000),
                "stream": request_data.get("stream", False)
            }
        
        else:  # generic
            return {
                "command": "generate",
                "prompt": request_data.get("prompt", "Hello"),
                "model": request_data.get("model", "default"),
                **(request_data.get("parameters", {}))
            }
    
    def _convert_from_internal_format(self, response_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
        """Convert internal response to API format"""
        content = response_data.get("response", "")
        
        if api_format == "openai":
            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": response_data.get("model", "gpt-3.5-turbo"),
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
                "model": response_data.get("model", "gpt-3.5-turbo"),
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
                "model": response_data.get("model", "claude-3-sonnet"),
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0
                }
            }
        
        else:  # generic
            return {
                "response": content,
                "model": response_data.get("model", "default"),
                "timestamp": time.time()
            }
    
    async def handle_request(self, request_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
        """Handle non-streaming request"""
        if not self.channel:
            raise HTTPException(status_code=503, detail="No inference service configured")
        
        self.request_count += 1
        
        # Convert to internal format
        internal_request = self._convert_to_internal_format(request_data, api_format)
        logger.info(f"Processing non-streaming request: {internal_request.get('prompt', '')[:50]}...")
        
        # Send to remote service
        result = await self.channel.send_request(internal_request)
        
        if not result.get("success"):
            raise HTTPException(status_code=500, detail="Remote inference failed")
        
        request_id = result.get("request_id")
        logger.info(f"Request sent with ID: {request_id}, waiting for response...")
        
        # Wait for the actual response - poll the task status
        max_wait_time = 60  # Wait up to 60 seconds
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            try:
                # Poll for response
                poll_response = await self._poll_for_response(request_id)
                
                if poll_response and "response" in poll_response:
                    actual_response = poll_response["response"]
                    logger.info(f"Received actual response: {actual_response[:100]}...")
                    
                    # Convert back to API format
                    response_data = {
                        "response": actual_response,
                        "model": internal_request.get("model", "default"),
                        "request_id": request_id
                    }
                    
                    return self._convert_from_internal_format(response_data, api_format)
                
                # Wait a bit before polling again
                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(1)
        
        # Timeout - return error
        logger.error(f"Timeout waiting for response to request {request_id}")
        raise HTTPException(status_code=504, detail="Timeout waiting for inference response")
    
    async def _poll_for_response(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Poll for response when streaming fails or for non-streaming requests"""
        return await self.channel._poll_for_response(request_id)
    
    async def handle_stream_request(self, request_data: Dict[str, Any], api_format: str) -> AsyncGenerator[str, None]:
        """Handle streaming request"""
        if not self.channel:
            yield f"data: {json.dumps({'error': 'No inference service configured'})}\n\n"
            return
        
        self.request_count += 1
        
        # Convert to internal format
        internal_request = self._convert_to_internal_format(request_data, api_format)
        logger.info(f"Starting streaming request: {internal_request.get('prompt', '')[:50]}...")
        
        try:
            # Send request first
            result = await self.channel.send_request(internal_request)
            
            if not result.get("success"):
                yield f"data: {json.dumps({'error': 'Remote inference failed'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            request_id = result.get("request_id")
            logger.info(f"Streaming request sent with ID: {request_id}, waiting for response...")
            
            # Since streaming has issues with initialization messages, 
            # let's use polling approach like non-streaming but send chunks
            max_wait_time = 60
            start_time = time.time()
            
            while time.time() - start_time < max_wait_time:
                try:
                    # Poll for response
                    poll_response = await self._poll_for_response(request_id)
                    
                    if poll_response and "response" in poll_response:
                        content = poll_response["response"]
                        logger.info(f"Received streaming response: {content[:100]}...")
                        
                        # Send the content as streaming chunks
                        if api_format == "openai":
                            # Split content into smaller chunks for streaming effect
                            words = content.split()
                            chunk_size = max(1, len(words) // 5)  # Split into ~5 chunks
                            
                            for i in range(0, len(words), chunk_size):
                                chunk_words = words[i:i + chunk_size]
                                chunk_content = " ".join(chunk_words)
                                if i > 0:  # Add space before subsequent chunks
                                    chunk_content = " " + chunk_content
                                
                                chunk = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": internal_request.get("model", "gpt-3.5-turbo"),
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"content": chunk_content},
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                                
                                # Small delay for streaming effect
                                await asyncio.sleep(0.1)
                            
                            # Send final chunk
                            final_chunk = {
                                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": internal_request.get("model", "gpt-3.5-turbo"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                        
                        elif api_format == "openai_responses":
                            # OpenAI Responses API streaming format
                            words = content.split()
                            chunk_size = max(1, len(words) // 5)
                            
                            for i in range(0, len(words), chunk_size):
                                chunk_words = words[i:i + chunk_size]
                                chunk_content = " ".join(chunk_words)
                                if i > 0:
                                    chunk_content = " " + chunk_content
                                
                                chunk = {
                                    "id": f"resp_{uuid.uuid4().hex[:24]}",
                                    "object": "response.delta",
                                    "created": int(time.time()),
                                    "model": internal_request.get("model", "gpt-3.5-turbo"),
                                    "delta": {
                                        "type": "text",
                                        "text": chunk_content
                                    }
                                }
                                yield f"data: {json.dumps(chunk)}\n\n"
                                await asyncio.sleep(0.1)
                            
                            # Final chunk
                            final_chunk = {
                                "id": f"resp_{uuid.uuid4().hex[:24]}",
                                "object": "response.completed",
                                "created": int(time.time()),
                                "model": internal_request.get("model", "gpt-3.5-turbo"),
                                "status": "completed"
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                        
                        elif api_format == "ollama_generate":
                            # Ollama Generate streaming format
                            words = content.split()
                            chunk_size = max(1, len(words) // 10)  # More chunks for Ollama
                            
                            for i in range(0, len(words), chunk_size):
                                chunk_words = words[i:i + chunk_size]
                                chunk_content = " ".join(chunk_words)
                                if i > 0:
                                    chunk_content = " " + chunk_content
                                
                                chunk = {
                                    "model": internal_request.get("model", "llama2"),
                                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                    "response": chunk_content,
                                    "done": False
                                }
                                yield json.dumps(chunk) + "\n"
                                await asyncio.sleep(0.05)  # Faster streaming for Ollama
                            
                            # Final chunk
                            final_chunk = {
                                "model": internal_request.get("model", "llama2"),
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                "response": "",
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
                            yield json.dumps(final_chunk) + "\n"
                        
                        elif api_format == "ollama_chat":
                            # Ollama Chat streaming format
                            words = content.split()
                            chunk_size = max(1, len(words) // 10)
                            
                            for i in range(0, len(words), chunk_size):
                                chunk_words = words[i:i + chunk_size]
                                chunk_content = " ".join(chunk_words)
                                if i > 0:
                                    chunk_content = " " + chunk_content
                                
                                chunk = {
                                    "model": internal_request.get("model", "llama2"),
                                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                    "message": {
                                        "role": "assistant",
                                        "content": chunk_content
                                    },
                                    "done": False
                                }
                                yield json.dumps(chunk) + "\n"
                                await asyncio.sleep(0.05)
                            
                            # Final chunk
                            final_chunk = {
                                "model": internal_request.get("model", "llama2"),
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                "message": {
                                    "role": "assistant",
                                    "content": ""
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
                            yield json.dumps(final_chunk) + "\n"
                        
                        elif api_format == "anthropic":
                            chunk = {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": content}
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        else:  # generic
                            yield f"data: {json.dumps({'content': content})}\n\n"
                        
                        # Send appropriate termination signal
                        if api_format in ["ollama_generate", "ollama_chat"]:
                            pass  # Ollama doesn't use [DONE], just the final chunk with done=true
                        else:
                            yield "data: [DONE]\n\n"
                        return
                    
                    # Wait a bit before polling again
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Polling error: {e}")
                    await asyncio.sleep(1)
            
            # Timeout
            logger.error(f"Timeout waiting for streaming response to request {request_id}")
            if api_format in ["ollama_generate", "ollama_chat"]:
                yield json.dumps({"error": "Timeout waiting for response"}) + "\n"
            else:
                yield f"data: {json.dumps({'error': 'Timeout waiting for response'})}\n\n"
                yield "data: [DONE]\n\n"
                
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            if api_format in ["ollama_generate", "ollama_chat"]:
                yield json.dumps({"error": str(e)}) + "\n"
            else:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                yield "data: [DONE]\n\n"
    
    def get_status(self) -> Dict[str, Any]:
        """Get proxy server status"""
        return {
            "status": "healthy" if self.health_status["healthy"] else "unhealthy",
            "mode": self.config.mode,
            "uptime": time.time() - self.start_time,
            "request_count": self.request_count,
            "remote_service": {
                "connected": self.channel is not None,
                "healthy": self.health_status["healthy"],
                "last_health_check": self.health_status["last_check"]
            }
        }

# ─────────────────────────── FastAPI App ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    proxy_server = app.state.proxy_server
    await proxy_server.start_health_monitor()
    logger.info("Proxy server started")
    yield
    # Shutdown
    logger.info("Proxy server shutting down")

def create_app(config: ProxyConfig) -> FastAPI:
    """Create FastAPI application"""
    
    app = FastAPI(
        title="Universal Inference Proxy",
        description="OpenAI/Anthropic/Responses/Generic API proxy for remote inference services",
        version="1.0.0",
        lifespan=lifespan
    )
    
    # Store proxy server in app state
    proxy_server = UniversalProxyServer(config)
    app.state.proxy_server = proxy_server
    
    # ─── OpenAI Chat Completions API Endpoints ───
    
    @app.post("/v1/chat/completions")
    async def openai_chat_completions(request: OpenAIChatRequest):
        """OpenAI Chat Completions API"""
        request_dict = request.model_dump()
        
        if request.stream:
            async def stream_generator():
                async for chunk in proxy_server.handle_stream_request(request_dict, "openai"):
                    yield chunk
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            result = await proxy_server.handle_request(request_dict, "openai")
            return JSONResponse(result)
    
    @app.get("/v1/models")
    async def openai_list_models():
        """OpenAI Models API"""
        return {
            "object": "list",
            "data": [
                {
                    "id": "gpt-3.5-turbo",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "proxy-server"
                },
                {
                    "id": "gpt-4",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "proxy-server"
                }
            ]
        }
    
    # ─── OpenAI Responses API Endpoints (New) ───
    
    @app.post("/v1/responses")
    async def openai_responses_create(request: OpenAIResponseRequest):
        """OpenAI Responses API - Create Response"""
        request_dict = request.model_dump()
        
        if request.stream:
            async def stream_generator():
                async for chunk in proxy_server.handle_stream_request(request_dict, "openai_responses"):
                    yield chunk
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            result = await proxy_server.handle_request(request_dict, "openai_responses")
            return JSONResponse(result)
    
    @app.get("/v1/responses/{response_id}")
    async def openai_responses_retrieve(response_id: str):
        """OpenAI Responses API - Retrieve Response"""
        # For now, return a mock response - in production you'd store responses
        return JSONResponse({
            "id": response_id,
            "object": "response", 
            "created": int(time.time()),
            "model": "gpt-3.5-turbo",
            "status": "completed",
            "output": [{"type": "text", "text": "Retrieved response content"}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        })
    
    @app.delete("/v1/responses/{response_id}")
    async def openai_responses_delete(response_id: str):
        """OpenAI Responses API - Delete Response"""
        return JSONResponse({
            "id": response_id,
            "object": "response.deleted",
            "deleted": True
        })
    
    # ─── Ollama API Endpoints (New) ───
    
    @app.post("/api/generate")
    async def ollama_generate(request: OllamaGenerateRequest):
        """Ollama Generate API"""
        request_dict = request.model_dump()
        
        if request.stream:
            return StreamingResponse(
                proxy_server.handle_stream_request(request_dict, "ollama_generate"),
                media_type="application/json"
            )
        else:
            result = await proxy_server.handle_request(request_dict, "ollama_generate")
            return JSONResponse(result)
    
    @app.post("/api/chat")
    async def ollama_chat(request: OllamaChatRequest):
        """Ollama Chat API"""
        request_dict = request.model_dump()
        
        if request.stream:
            return StreamingResponse(
                proxy_server.handle_stream_request(request_dict, "ollama_chat"),
                media_type="application/json"
            )
        else:
            result = await proxy_server.handle_request(request_dict, "ollama_chat")
            return JSONResponse(result)
    
    @app.get("/api/tags")
    async def ollama_list_models():
        """Ollama List Models API"""
        return {
            "models": [
                {
                    "name": "llama2:latest",
                    "modified_at": "2024-01-01T00:00:00.000000000Z",
                    "size": 3825819519,
                    "digest": "sha256:abc123def456",
                    "details": {
                        "format": "gguf",
                        "family": "llama",
                        "parameter_size": "7B",
                        "quantization_level": "Q4_0"
                    }
                },
                {
                    "name": "llama2:13b",
                    "modified_at": "2024-01-01T00:00:00.000000000Z",
                    "size": 7365960935,
                    "digest": "sha256:def456ghi789",
                    "details": {
                        "format": "gguf",
                        "family": "llama",
                        "parameter_size": "13B",
                        "quantization_level": "Q4_0"
                    }
                }
            ]
        }
    
    @app.post("/api/show")
    async def ollama_show_model(request: Dict[str, str]):
        """Ollama Show Model API"""
        model_name = request.get("name", "llama2")
        return {
            "license": "LLAMA 2 COMMUNITY LICENSE AGREEMENT",
            "modelfile": f"# Modelfile generated by Ollama\nFROM /path/to/{model_name}\n",
            "parameters": "num_ctx 2048\nstop \"<|im_end|>\"\nstop \"<|im_start|>\"",
            "template": "{{ if .System }}<|im_start|>system\n{{ .System }}<|im_end|>\n{{ end }}{{ if .Prompt }}<|im_start|>user\n{{ .Prompt }}<|im_end|>\n{{ end }}<|im_start|>assistant\n",
            "details": {
                "format": "gguf",
                "family": "llama",
                "families": ["llama"],
                "parameter_size": "7B",
                "quantization_level": "Q4_0"
            }
        }
    
    # ─── Anthropic API Endpoints ───
    
    @app.post("/v1/messages")
    async def anthropic_messages(request: AnthropicRequest):
        """Anthropic Messages API"""
        request_dict = request.model_dump()
        
        if request.stream:
            async def stream_generator():
                async for chunk in proxy_server.handle_stream_request(request_dict, "anthropic"):
                    yield chunk
            
            return StreamingResponse(
                stream_generator(),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            result = await proxy_server.handle_request(request_dict, "anthropic")
            return JSONResponse(result)
    
    # ─── Generic API Endpoints ───
    
    @app.post("/generate")
    async def generic_generate(request: GenericRequest):
        """Generic generation endpoint"""
        request_dict = request.model_dump()
        result = await proxy_server.handle_request(request_dict, "generic")
        return JSONResponse(result)
    
    @app.post("/stream")
    async def generic_stream(request: GenericRequest):
        """Generic streaming endpoint"""
        request_dict = request.model_dump()
        
        async def stream_generator():
            async for chunk in proxy_server.handle_stream_request(request_dict, "generic"):
                yield chunk
        
        return StreamingResponse(
            stream_generator(),
            media_type="text/plain",
            headers={"Cache-Control": "no-cache"}
        )
    
    # ─── Status and Health ───
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        status = proxy_server.get_status()
        status_code = 200 if status["status"] == "healthy" else 503
        return JSONResponse(status, status_code=status_code)
    
    @app.get("/status")
    async def get_status():
        """Detailed status endpoint"""
        return JSONResponse(proxy_server.get_status())
    
    return app

# ─────────────────────────── Main ──────────────────────────────────────────

def load_aes_key(key_path: str) -> bytes:
    """Load AES key from file"""
    try:
        raw = Path(key_path).read_bytes().strip()
        try:
            return base64.b64decode(raw)
        except:
            return raw
    except Exception as e:
        raise ValueError(f"Failed to load AES key: {e}")

def main():
    parser = argparse.ArgumentParser(description="Universal Inference Proxy Server")
    parser.add_argument("--mode", choices=["openai", "anthropic", "generic"], default="openai",
                       help="API compatibility mode")
    parser.add_argument("--inference-task", required=True, help="Remote inference task ID")
    parser.add_argument("--server", default="http://localhost:9000", help="Pixi runner server URL")
    parser.add_argument("--aes-key", help="AES key file path")
    parser.add_argument("--auth-token", help="Authentication token")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load AES key
    aes_key = None
    if args.aes_key:
        aes_key = load_aes_key(args.aes_key)
        logger.info("AES encryption enabled")
    
    # Setup auth headers
    auth_headers = {}
    if args.auth_token:
        auth_headers["Authorization"] = f"Bearer {args.auth_token}"
    
    # Create configuration
    config = ProxyConfig(
        mode=args.mode,
        inference_task_id=args.inference_task,
        server_url=args.server,
        aes_key=aes_key,
        auth_headers=auth_headers
    )
    
    # Create and run app
    app = create_app(config)
    
    logger.info(f"Starting proxy server in {args.mode} mode")
    logger.info(f"Remote inference task: {args.inference_task}")
    logger.info(f"Server: {args.host}:{args.port}")
    
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
