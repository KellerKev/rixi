#!/usr/bin/env python3
"""
Enhanced Universal Proxy Server
Provides OpenAI, Anthropic, Responses API, and generic HTTP API compatibility
for remote inference services via secure Pixi runner infrastructure.

Features:
- Model mapping from configuration
- Optional JWT authentication for health endpoints
- Friendly error messages for unavailable models
- Production-ready with metrics and caching
- Automatic AES response decryption
- Configurable response handling
"""

import asyncio
import argparse
import base64
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List, AsyncGenerator
from pathlib import Path
import statistics

import uvicorn
import jwt
import yaml  # Use YAML instead of TOML
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import aiohttp

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
    input: str
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
    images: Optional[List[str]] = None
    format: Optional[str] = None
    options: Optional[Dict[str, Any]] = None
    system: Optional[str] = None
    template: Optional[str] = None
    context: Optional[List[int]] = None
    stream: Optional[bool] = True
    raw: Optional[bool] = False
    keep_alive: Optional[str] = "5m"

class OllamaChatMessage(BaseModel):
    role: str
    content: str
    images: Optional[List[str]] = None

class OllamaChatRequest(BaseModel):
    model: str
    messages: List[OllamaChatMessage]
    tools: Optional[List[Dict[str, Any]]] = None
    format: Optional[str] = None
    options: Optional[Dict[str, Any]] = None
    stream: Optional[bool] = True
    keep_alive: Optional[str] = "5m"

class GenericRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None

# ─────────────────────────── Configuration Loading ─────────────────────────

def load_config_from_file(config_path: str) -> Dict[str, Any]:
    """Load configuration from YAML or JSON file"""
    config_file = Path(config_path)
    
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    content = config_file.read_text()
    
    if config_path.endswith('.json'):
        return json.loads(content)
    elif config_path.endswith(('.yaml', '.yml')):
        return yaml.safe_load(content)
    else:
        raise ValueError("Config file must be JSON or YAML")

def create_default_config() -> Dict[str, Any]:
    """Create default configuration"""
    return {
        "backends": [
            {
                "id": "backend1",
                "url": "http://localhost:9000/task/inference-task-1",
                "weight": 1
            }
        ],
        "model_mapping": {
            "enabled": True,
            "mappings": {
                "gpt-3.5-turbo": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "gpt-4": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "gpt-4-turbo": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "claude-3-sonnet-20240229": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "claude-3-opus-20240229": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "llama2": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "llama3": "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
            }
        },
        "available_models": [
            "TinyLlama/TinyLlama-1.1B-Chat-v1.0"
        ],
        "authentication": {
            "require_api_key": False,
            "api_key": None,
            "jwt": {
                "enabled": False,
                "secret_key": None,
                "algorithm": "HS256",
                "health_endpoint_auth": False
            }
        },
        "cache_max_size": 1000,
        "cache_ttl_seconds": 3600,
        "cors_origins": [
            "http://localhost:3000",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8080"
        ],
        "aes_key": None,
        "auth_headers": {
            # Default auth headers for server communication
            # The server often requires authentication when AES is enabled
        },
        # NEW: Response handling configuration
        "response_handling": {
            "decrypt_responses": True,  # Automatically decrypt AES-encrypted responses
            "force_json_content_type": True,  # Convert content-type to application/json for JSON responses
            "preserve_original_headers": False  # Whether to keep all original headers or clean them
        }
    }

# ─────────────────────────── Authentication ─────────────────────────────────

security = HTTPBearer(auto_error=False)

class JWTAuthenticator:
    def __init__(self, config: Dict[str, Any]):
        self.config = config.get("authentication", {})
        self.jwt_config = self.config.get("jwt", {})
        self.enabled = self.jwt_config.get("enabled", False)
        self.secret_key = self.jwt_config.get("secret_key")
        self.algorithm = self.jwt_config.get("algorithm", "HS256")
        self.health_auth = self.jwt_config.get("health_endpoint_auth", False)
        
    def verify_token(self, token: str) -> bool:
        """Verify JWT token"""
        if not self.enabled or not self.secret_key:
            return True
            
        try:
            jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            return True
        except jwt.PyJWTError:
            return False
    
    def optional_verify(self, authorization: Optional[HTTPAuthorizationCredentials] = Depends(security)):
        """Optional JWT verification for health endpoints"""
        if not self.health_auth:
            return True
            
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header required")
            
        if not self.verify_token(authorization.credentials):
            raise HTTPException(status_code=401, detail="Invalid JWT token")
            
        return True

# ─────────────────────────── Metrics & Caching ─────────────────────────────

class SimpleCache:
    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.cache = {}
        
    def _make_key(self, request_data: Dict[str, Any]) -> str:
        # Create a cache key from request data - use full prompt for better specificity
        cache_dict = {
            "prompt": request_data.get("prompt", ""),  # Use FULL prompt, not just first 100 chars
            "model": request_data.get("model", ""),
            "temperature": request_data.get("temperature", 0.7),
            "max_tokens": request_data.get("max_tokens"),
            "stream": request_data.get("stream", False)
        }
        return base64.b64encode(json.dumps(cache_dict, sort_keys=True).encode()).decode()
    
    def get(self, request_data: Dict[str, Any]) -> Optional[str]:
        key = self._make_key(request_data)
        entry = self.cache.get(key)
        
        if entry and time.time() - entry["timestamp"] < self.ttl_seconds:
            return entry["response"]
        elif entry:
            del self.cache[key]  # Expired
        return None
    
    def set(self, request_data: Dict[str, Any], response: str):
        if len(self.cache) >= self.max_size:
            # Remove oldest entry
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]["timestamp"])
            del self.cache[oldest_key]
        
        key = self._make_key(request_data)
        self.cache[key] = {
            "response": response,
            "timestamp": time.time()
        }
    
    def clear(self):
        self.cache.clear()

class MetricsCollector:
    def __init__(self):
        self.start_time = time.time()
        self.request_count = 0
        self.error_count = 0
        self.response_times = []
        self.requests_per_minute = []
        self.last_minute = int(time.time() // 60)
        self.current_minute_requests = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.model_usage = {}
        
    def record_request(self, model: str, response_time: float, success: bool):
        self.request_count += 1
        current_minute = int(time.time() // 60)
        
        if current_minute != self.last_minute:
            self.requests_per_minute.append(self.current_minute_requests)
            if len(self.requests_per_minute) > 60:
                self.requests_per_minute.pop(0)
            self.current_minute_requests = 0
            self.last_minute = current_minute
        
        self.current_minute_requests += 1
        self.response_times.append(response_time)
        if len(self.response_times) > 1000:
            self.response_times.pop(0)
        
        if not success:
            self.error_count += 1
            
        # Track model usage
        if model not in self.model_usage:
            self.model_usage[model] = 0
        self.model_usage[model] += 1
    
    def record_cache_hit(self):
        self.cache_hits += 1
    
    def record_cache_miss(self):
        self.cache_misses += 1
    
    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time
    
    @property
    def avg_response_time(self) -> float:
        return statistics.mean(self.response_times) if self.response_times else 0.0
    
    @property
    def error_rate(self) -> float:
        return self.error_count / self.request_count if self.request_count > 0 else 0.0
    
    @property
    def cache_hit_ratio(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total > 0 else 0.0

# ─────────────────────────── Remote Channel ─────────────────────────────────

class RemoteChannel:
    def __init__(self, backend_url: str, aes_key: Optional[bytes] = None, auth_headers: Dict[str, str] = None, response_config: Dict[str, Any] = None):
        self.backend_url = backend_url
        self.aes_key = aes_key
        self.auth_headers = auth_headers or {}
        # NEW: Response handling configuration
        self.response_config = response_config or {
            "decrypt_responses": True,
            "force_json_content_type": True,
            "preserve_original_headers": False
        }
        
    def send_frame(self, data: bytes) -> bytes:
        """Send frame in server's expected format: length prefix + encrypted data"""
        enc = self.encrypt(data)
        return len(enc).to_bytes(4, "big") + enc
        
    def encrypt(self, data: bytes) -> bytes:
        """Encrypt data using AES-GCM (matching server format)"""
        if not self.aes_key:
            return data
        nonce = os.urandom(12)  # 12-byte nonce for AES-GCM
        return nonce + AESGCM(self.aes_key).encrypt(nonce, data, None)
    
    def decrypt(self, data: bytes) -> bytes:
        """Decrypt data using AES-GCM (matching server format)"""
        if not self.aes_key:
            return data
        return AESGCM(self.aes_key).decrypt(data[:12], data[12:], None)
    
    def _process_response(self, response_content: bytes, response_headers: Dict[str, str]) -> tuple[bytes, Dict[str, str]]:
        """Process response content and headers based on configuration"""
        
        # Step 1: Handle AES decryption
        if self.response_config.get("decrypt_responses", True) and self.aes_key:
            # More robust content-type detection for encrypted responses
            content_type = response_headers.get("content-type", "").lower()
            is_encrypted = (
                content_type == "application/octet-stream" or
                content_type.startswith("application/octet-stream") or
                # Also check if content looks like encrypted binary data
                (len(response_content) > 12 and not response_content.startswith(b'{') and not response_content.startswith(b'<'))
            )
            
            if is_encrypted:
                try:
                    logger.info(f"🔓 Decrypting AES-encrypted response (detected content-type: {response_headers.get('content-type', 'none')})")
                    decrypted_content = self.decrypt(response_content)
                    response_content = decrypted_content
                    
                    # Step 2: Auto-detect content type after decryption
                    if self.response_config.get("force_json_content_type", True):
                        try:
                            # Try to parse as JSON to verify it's valid JSON
                            json.loads(decrypted_content.decode('utf-8'))
                            response_headers["content-type"] = "application/json"
                            logger.info("✅ Detected JSON content, setting content-type to application/json")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # Not JSON, try to detect other types
                            if decrypted_content.startswith(b'<'):
                                response_headers["content-type"] = "text/html"
                                logger.info("✅ Detected HTML content")
                            elif decrypted_content.startswith(b'{') or decrypted_content.startswith(b'['):
                                response_headers["content-type"] = "application/json"
                                logger.info("✅ Detected JSON-like content")
                            else:
                                response_headers["content-type"] = "text/plain"
                                logger.info("✅ Detected plain text content")
                    else:
                        # Use basic detection without forcing JSON
                        if decrypted_content.startswith(b'{') or decrypted_content.startswith(b'['):
                            response_headers["content-type"] = "application/json"
                        elif decrypted_content.startswith(b'<'):
                            response_headers["content-type"] = "text/html"
                        else:
                            response_headers["content-type"] = "text/plain"
                    
                except Exception as e:
                    logger.warning(f"⚠️ Failed to decrypt response: {e}")
                    # Keep original encrypted content
                    pass
            else:
                logger.info(f"ℹ️ Response not encrypted (content-type: {response_headers.get('content-type', 'none')}, content preview: {response_content[:50]})")
        else:
            if self.aes_key and not self.response_config.get("decrypt_responses", True):
                logger.info("🔒 AES decryption disabled by configuration - passing through encrypted response")
            else:
                logger.info("ℹ️ No AES key configured - passing through unencrypted response")
        
        # Step 3: Clean response headers
        if not self.response_config.get("preserve_original_headers", False):
            hop_by_hop = {'connection', 'keep-alive', 'proxy-authenticate', 
                         'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade'}
            response_headers = {k: v for k, v in response_headers.items() if k.lower() not in hop_by_hop}
            
            # Remove problematic headers that might cause conflicts
            response_headers.pop("content-length", None)  # Let FastAPI calculate this
            response_headers.pop("Content-Length", None)
        
        return response_content, response_headers
    
    async def send_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        """Send request to Pixi runner backend (matching server expectations)"""
        try:
            async with aiohttp.ClientSession() as session:
                data = json.dumps(request_data).encode()
                
                # Match server's expected format
                if self.aes_key:
                    # For /task/{id}/input endpoint, server expects direct encrypted data (no length prefix)
                    encrypted_data = self.encrypt(data)
                    headers = {"Content-Type": "application/octet-stream"}
                    logger.info(f"Sending encrypted request ({len(encrypted_data)} bytes)")
                else:
                    # Server expects plain JSON for non-encrypted
                    encrypted_data = data
                    headers = {"Content-Type": "application/json"}
                    logger.info(f"Sending unencrypted request ({len(data)} bytes)")
                
                # Add authentication headers (server requires auth by default with AES)
                headers.update(self.auth_headers)
                logger.info(f"Using headers: {list(headers.keys())}")
                
                # Send to the Pixi runner's input endpoint
                input_url = f"{self.backend_url}/input"
                logger.info(f"Sending request to: {input_url}")
                
                async with session.post(
                    input_url,
                    data=encrypted_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    response_text = await response.text()
                    logger.info(f"Input response: HTTP {response.status}, body: {response_text[:200]}...")
                    
                    if response.status == 200:
                        # Extract task ID from the backend URL
                        if "/task/" in self.backend_url:
                            task_id = self.backend_url.split("/task/")[-1]
                            logger.info(f"✅ Request sent successfully, task ID: {task_id}")
                            return {"success": True, "task_id": task_id}
                        else:
                            logger.error("❌ No task ID found in backend URL")
                            return {"success": False, "error": "No task ID in backend URL"}
                    else:
                        logger.error(f"❌ Input request failed: HTTP {response.status}")
                        logger.error(f"   Response: {response_text}")
                        return {"success": False, "error": f"HTTP {response.status}: {response_text}"}
                        
        except Exception as e:
            logger.error(f"❌ Backend request failed: {e}")
            return {"success": False, "error": str(e)}
    
    async def poll_for_response(self, task_id: str, max_wait_time: int = 60) -> Optional[Dict[str, Any]]:
        """Poll the actual task status endpoint for response"""
        base_url = self.backend_url.rsplit("/task/", 1)[0]  # Get base URL
        status_url = f"{base_url}/task/{task_id}"
        
        start_time = time.time()
        logger.info(f"Starting to poll task {task_id} at {status_url}")
        logger.info(f"Using auth headers: {list(self.auth_headers.keys())}")
        
        poll_count = 0
        initial_output_count = None
        target_request_timestamp = start_time  # Use poll start time as reference
        
        async with aiohttp.ClientSession() as session:
            while time.time() - start_time < max_wait_time:
                poll_count += 1
                try:
                    headers = dict(self.auth_headers)
                    
                    async with session.get(status_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        logger.debug(f"Poll #{poll_count}: HTTP {response.status}")
                        
                        if response.status == 200:
                            task_info = await response.json()
                            
                            # Debug: Show what we got
                            status = task_info.get("status", "unknown")
                            logger.info(f"Poll #{poll_count}: Task {task_id} status: {status}")
                            
                            # Look for output in recent_output
                            recent_output = task_info.get("recent_output", [])
                            
                            # On first poll, record how many outputs existed before our request
                            if initial_output_count is None:
                                initial_output_count = len(recent_output)
                                logger.info(f"📊 Initial output count: {initial_output_count}")
                            
                            logger.info(f"Found {len(recent_output)} total output items")
                            
                            # Look for NEW inference responses (after our request started)
                            if len(recent_output) > initial_output_count:
                                # Check outputs in REVERSE order (newest first)
                                new_outputs = recent_output[initial_output_count:]
                                logger.info(f"🔍 Checking {len(new_outputs)} new outputs (newest first)...")
                                
                                # Search from newest to oldest
                                for i in reversed(range(len(new_outputs))):
                                    output = new_outputs[i]
                                    content = output.get("content", "").strip()
                                    output_type = output.get("type", "")
                                    
                                    # Only look at stdout output
                                    if output_type == "output" and content:
                                        logger.info(f"🔍 Checking output {i} (reverse order) for inference response...")
                                        
                                        # Check if content contains inference response JSON
                                        if "request_id" in content and "response" in content and content.startswith("{"):
                                            try:
                                                # Parse the JSON inference response
                                                inference_response = json.loads(content)
                                                if "response" in inference_response and "request_id" in inference_response:
                                                    response_text = inference_response["response"]
                                                    request_id = inference_response["request_id"]
                                                    timestamp = inference_response.get("timestamp", 0)
                                                    
                                                    # Only return responses that came after our request
                                                    if timestamp >= target_request_timestamp:
                                                        logger.info(f"🎯 Found FRESH inference response! Request ID: {request_id}")
                                                        logger.info(f"   Timestamp: {timestamp} (target: {target_request_timestamp})")
                                                        logger.info(f"   Response length: {len(response_text)} chars")
                                                        logger.info(f"   Content preview: '{response_text[:200]}...'")
                                                        return {
                                                            "response": response_text,
                                                            "model": inference_response.get("model"),
                                                            "request_id": request_id
                                                        }
                                                    else:
                                                        logger.info(f"⏳ Skipping old response (timestamp: {timestamp})")
                                            except json.JSONDecodeError:
                                                logger.info("❌ Failed to parse suspected inference JSON")
                                                continue
                            
                            # Check if task has completed or failed (fallback)
                            if status in ["completed", "failed"] and poll_count > 5:
                                logger.info(f"🎉 Task {task_id} finished with status: {status}")
                                
                                # Look for the LATEST response by timestamp
                                latest_response = None
                                latest_timestamp = 0
                                
                                for output in recent_output:
                                    content = output.get("content", "").strip()
                                    output_type = output.get("type", "")
                                    
                                    if output_type == "output" and content and "request_id" in content and "response" in content:
                                        try:
                                            inference_response = json.loads(content)
                                            if "response" in inference_response and "request_id" in inference_response:
                                                timestamp = inference_response.get("timestamp", 0)
                                                if timestamp > latest_timestamp and timestamp >= target_request_timestamp:
                                                    latest_response = inference_response
                                                    latest_timestamp = timestamp
                                        except json.JSONDecodeError:
                                            continue
                                
                                if latest_response:
                                    response_text = latest_response["response"]
                                    logger.info(f"🎯 Using latest response by timestamp: {response_text[:100]}...")
                                    return {
                                        "response": response_text,
                                        "model": latest_response.get("model"),
                                        "request_id": latest_response.get("request_id")
                                    }
                                
                                return {"response": f"Task {status} but no fresh response found", "error": "No response content"}
                            
                            # Task still running, continue polling
                            if poll_count % 10 == 0:  # Log every 10 polls
                                logger.info(f"⏳ Task {task_id} still running (status: {status}), poll #{poll_count}")
                        
                        elif response.status == 404:
                            logger.error(f"❌ Task {task_id} not found (404)")
                            return {"response": "Task not found", "error": "Task not found"}
                        elif response.status == 401:
                            logger.error(f"❌ Authentication failed (401)")
                            return {"response": "Authentication failed", "error": "Unauthorized"}
                        else:
                            logger.warning(f"⚠️  Task status request failed: HTTP {response.status}")
                            response_text = await response.text()
                            logger.warning(f"   Response: {response_text[:200]}...")
                            
                        await asyncio.sleep(2)  # Wait 2 seconds before next poll
                        
                except asyncio.TimeoutError:
                    logger.warning(f"⏰ Timeout polling task {task_id} (poll #{poll_count})")
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"💥 Polling error for task {task_id} (poll #{poll_count}): {e}")
                    await asyncio.sleep(2)
        
        logger.error(f"❌ Timeout waiting for task {task_id} response after {max_wait_time}s and {poll_count} polls")
        return None  # Timeout
    
    async def forward_http_request(self, request: Request, path: str, task_id: str = None) -> Dict[str, Any]:
        """Forward generic HTTP request to backend service with enhanced response handling"""
        try:
            async with aiohttp.ClientSession() as session:
                # Build target URL - if task_id provided, use task-specific endpoint
                if task_id:
                    base_url = self.backend_url.rsplit("/task/", 1)[0]
                    target_url = f"{base_url}/task/{task_id}/proxy/{path}"
                else:
                    # Extract task ID from backend URL for backwards compatibility
                    if "/task/" in self.backend_url:
                        task_id = self.backend_url.split("/task/")[-1]
                        base_url = self.backend_url.rsplit("/task/", 1)[0]
                        target_url = f"{base_url}/task/{task_id}/proxy/{path}"
                    else:
                        target_url = f"{self.backend_url}/proxy/{path}"
                
                # Get request data
                method = request.method
                query_params = dict(request.query_params)

                # Only forward a safe whitelist of client headers; never forward
                # client auth headers (Authorization, cookies, x-api-key, ...)
                allowed_headers = {'content-type', 'accept', 'user-agent', 'x-request-id'}
                headers = {k: v for k, v in request.headers.items() if k.lower() in allowed_headers}

                # Add auth headers
                headers.update(self.auth_headers)
                
                # Get request body
                try:
                    if method in ['POST', 'PUT', 'PATCH']:
                        body = await request.body()
                        if self.aes_key and body:
                            # Encrypt body if AES is enabled
                            body = self.encrypt(body)
                            headers["Content-Type"] = "application/octet-stream"
                    else:
                        body = None
                except Exception:
                    body = None
                
                logger.info(f"Forwarding {method} {target_url} with {len(query_params)} params")
                
                # Make the request
                async with session.request(
                    method=method,
                    url=target_url,
                    params=query_params,
                    headers=headers,
                    data=body,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    
                    # Get response data
                    response_content = await response.read()
                    response_headers = dict(response.headers)
                    
                    # NEW: Enhanced response processing with configuration
                    response_content, response_headers = self._process_response(
                        response_content, response_headers
                    )
                    
                    logger.info(f"Backend response: {response.status} ({len(response_content)} bytes)")
                    
                    return {
                        "success": True,
                        "status_code": response.status,
                        "content": response_content,
                        "headers": response_headers,
                        "media_type": response_headers.get("content-type", "application/octet-stream")
                    }
                    
        except Exception as e:
            logger.error(f"HTTP forwarding error: {e}")
            return {
                "success": False,
                "error": str(e),
                "status_code": 500
            }
    
    async def stream_response(self, task_id: str) -> AsyncGenerator[str, None]:
        """Stream response from task output with proper length-prefixed frame handling"""
        base_url = self.backend_url.rsplit("/task/", 1)[0]
        stream_url = f"{base_url}/task/{task_id}/stream"
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = dict(self.auth_headers)
                
                async with session.get(stream_url, headers=headers) as response:
                    if response.status == 200:
                        buffer = b""
                        need_bytes = None
                        
                        async for chunk in response.content.iter_any():
                            buffer += chunk
                            
                            while True:
                                # Read 4-byte length prefix
                                if need_bytes is None and len(buffer) >= 4:
                                    need_bytes = int.from_bytes(buffer[:4], "big")
                                    buffer = buffer[4:]
                                
                                # Check if we have enough data for current frame
                                if need_bytes is None or len(buffer) < need_bytes:
                                    break
                                
                                # Extract frame and process
                                frame_data, buffer = buffer[:need_bytes], buffer[need_bytes:]
                                need_bytes = None
                                
                                try:
                                    # Decrypt the frame if AES is enabled
                                    if self.aes_key:
                                        decrypted_data = self.decrypt(frame_data)
                                        line_str = decrypted_data.decode('utf-8')
                                    else:
                                        line_str = frame_data.decode('utf-8')
                                    
                                    # Process each line in the decrypted data
                                    for line in line_str.split('\n'):
                                        if not line.strip():
                                            continue
                                        try:
                                            parsed = json.loads(line)
                                            if "output" in parsed:
                                                yield parsed["output"]
                                            elif "stderr" in parsed:
                                                yield parsed["stderr"]
                                            elif "response" in parsed:
                                                yield parsed["response"]
                                        except json.JSONDecodeError:
                                            # If not JSON, yield the line as is
                                            yield line.strip()
                                            
                                except Exception as e:
                                    logger.warning(f"Frame processing error: {e}")
                                    continue
                    else:
                        logger.error(f"Stream request failed with status {response.status}")
                        
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"Error streaming response: {e}"

# ─────────────────────────── Proxy Server ───────────────────────────────────

class ProxyServer:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.backends = config.get("backends", [])
        self.model_mapping_config = config.get("model_mapping", {})
        self.available_models = config.get("available_models", [])
        self.cache = SimpleCache(
            max_size=config.get("cache_max_size", 1000),
            ttl_seconds=config.get("cache_ttl_seconds", 3600)
        )
        self.metrics = MetricsCollector()
        self.auth = JWTAuthenticator(config)
        
        # Setup channels to backends
        self.channels = []
        backend_auth_headers = config.get("auth_headers", {})
        response_config = config.get("response_handling", {})
        
        # Log authentication and response handling setup for debugging
        logger.info(f"Backend auth headers configured: {list(backend_auth_headers.keys())}")
        logger.info(f"Response handling config: {response_config}")
        
        for backend in self.backends:
            channel = RemoteChannel(
                backend["url"],
                config.get("aes_key"),
                backend_auth_headers,
                response_config  # NEW: Pass response config to channel
            )
            self.channels.append(channel)
            logger.info(f"Created channel for backend: {backend['url']}")
        
        logger.info(f"Initialized proxy with {len(self.channels)} backends")
    
    def map_model_name(self, requested_model: str) -> tuple[str, bool]:
        """
        Map model name and return (mapped_name, is_valid)
        
        Returns:
            tuple: (mapped_model_name, is_model_available)
        """
        if not self.model_mapping_config.get("enabled", True):
            # Model mapping disabled - check if model is in available list
            if requested_model in self.available_models:
                return requested_model, True
            else:
                return requested_model, False
        
        # Model mapping enabled
        mappings = self.model_mapping_config.get("mappings", {})
        mapped_model = mappings.get(requested_model, requested_model)
        
        # Check if mapped model is available
        is_available = mapped_model in self.available_models
        
        if mapped_model != requested_model:
            logger.info(f"Mapped model '{requested_model}' -> '{mapped_model}'")
        
        return mapped_model, is_available
    
    def get_model_error_response(self, requested_model: str, api_format: str) -> Dict[str, Any]:
        """Generate friendly error response for unavailable models"""
        mapped_model, is_available = self.map_model_name(requested_model)
        
        if self.model_mapping_config.get("enabled", True):
            error_msg = f"Model '{requested_model}' is not available. Available models: {', '.join(self.available_models)}"
        else:
            error_msg = f"Model '{requested_model}' is not supported. Available models: {', '.join(self.available_models)}"
        
        if api_format == "openai":
            return {
                "error": {
                    "message": error_msg,
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }
        elif api_format == "openai_responses":
            return {
                "error": {
                    "message": error_msg,
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }
        elif api_format == "anthropic":
            return {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": error_msg
                }
            }
        elif api_format in ["ollama_generate", "ollama_chat"]:
            return {
                "error": error_msg,
                "available_models": self.available_models
            }
        else:  # generic
            return {
                "error": error_msg,
                "available_models": self.available_models
            }
    
    def _convert_to_internal_format(self, request_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
        """Convert API format to internal format"""
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
            
            # Map model name
            requested_model = request_data.get("model", "gpt-3.5-turbo")
            mapped_model, is_available = self.map_model_name(requested_model)
            
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
            mapped_model, is_available = self.map_model_name(requested_model)
            
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
            mapped_model, is_available = self.map_model_name(requested_model)
            
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
            mapped_model, is_available = self.map_model_name(requested_model)
            
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
            mapped_model, is_available = self.map_model_name(requested_model)
            
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
            mapped_model, is_available = self.map_model_name(requested_model)
            
            return {
                "command": "generate",
                "prompt": request_data.get("prompt", "Hello"),
                "model": mapped_model,
                "model_available": is_available,
                "original_model": requested_model,
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
    
    async def handle_request(self, request_data: Dict[str, Any], api_format: str) -> Dict[str, Any]:
        """Handle non-streaming request"""
        start_time = time.time()
        success = False
        
        try:
            # Convert to internal format
            internal_request = self._convert_to_internal_format(request_data, api_format)
            
            # Check if model is available
            if not internal_request.get("model_available", True):
                return self.get_model_error_response(internal_request["original_model"], api_format)
            
            # Check cache for non-streaming requests
            if not internal_request.get("stream", False):
                cached_response = self.cache.get(internal_request)
                if cached_response:
                    self.metrics.record_cache_hit()
                    response_data = {
                        "response": cached_response,
                        "original_model": internal_request.get("original_model", "default")
                    }
                    success = True
                    self.metrics.record_request(internal_request.get("model", "default"), time.time() - start_time, success)
                    return self._convert_from_internal_format(response_data, api_format)
                else:
                    self.metrics.record_cache_miss()
            
            logger.info(f"Processing request: {internal_request.get('prompt', '')[:50]}...")
            
            # Use first available channel (simple round-robin could be added)
            if not self.channels:
                raise HTTPException(status_code=503, detail="No backends available")
            
            channel = self.channels[0]
            result = await channel.send_request(internal_request)
            
            if not result.get("success"):
                raise HTTPException(status_code=500, detail="Backend inference failed")
            
            # Poll for response using the actual task ID
            task_id = result.get("task_id")
            if not task_id:
                raise HTTPException(status_code=500, detail="No task ID received from backend")
                
            response = await channel.poll_for_response(task_id)
            
            if response and "response" in response:
                actual_response = response["response"]
                
                # Cache the response
                if not internal_request.get("stream", False):
                    self.cache.set(internal_request, actual_response)
                
                response_data = {
                    "response": actual_response,
                    "original_model": internal_request.get("original_model", "default")
                }
                
                success = True
                return self._convert_from_internal_format(response_data, api_format)
            else:
                raise HTTPException(status_code=504, detail="Timeout waiting for response")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Request processing error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            self.metrics.record_request(
                internal_request.get("model", "default") if 'internal_request' in locals() else "unknown",
                time.time() - start_time,
                success
            )
    
    async def handle_streaming_request(self, request_data: Dict[str, Any], api_format: str) -> AsyncGenerator[str, None]:
        """Handle streaming request"""
        start_time = time.time()
        success = False
        
        try:
            # Convert to internal format
            internal_request = self._convert_to_internal_format(request_data, api_format)
            
            # Check if model is available
            if not internal_request.get("model_available", True):
                error_response = self.get_model_error_response(internal_request["original_model"], api_format)
                yield f"data: {json.dumps(error_response)}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            logger.info(f"Processing streaming request: {internal_request.get('prompt', '')[:50]}...")
            
            # Use first available channel
            if not self.channels:
                yield f"data: {json.dumps({'error': 'No backends available'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            channel = self.channels[0]
            result = await channel.send_request(internal_request)
            
            if not result.get("success"):
                yield f"data: {json.dumps({'error': 'Backend inference failed'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            # Get the task ID for streaming
            task_id = result.get("task_id")
            
            # For now, let's poll for the complete response and then stream it in chunks
            # In the future, you could use the real streaming endpoint
            response = await channel.poll_for_response(task_id)
            
            if response and "response" in response:
                actual_response = response["response"]
                success = True
                
                # Stream the response in chunks
                if api_format == "openai":
                    # Split response into chunks for streaming effect
                    words = actual_response.split()
                    chunk_size = max(1, len(words) // 8)  # Create ~8 chunks
                    
                    for i in range(0, len(words), chunk_size):
                        chunk_words = words[i:i + chunk_size]
                        chunk_content = " ".join(chunk_words)
                        if i > 0:  # Add space before subsequent chunks
                            chunk_content = " " + chunk_content
                        
                        chunk = {
                            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": internal_request.get("original_model", "gpt-3.5-turbo"),
                            "choices": [{
                                "index": 0,
                                "delta": {"content": chunk_content},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        await asyncio.sleep(0.1)  # Small delay for streaming effect
                    
                    # Final chunk
                    final_chunk = {
                        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                        "object": "chat.completion.chunk", 
                        "created": int(time.time()),
                        "model": internal_request.get("original_model", "gpt-3.5-turbo"),
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                
                elif api_format == "openai_responses":
                    # OpenAI Responses API streaming format
                    words = actual_response.split()
                    chunk_size = max(1, len(words) // 8)
                    
                    for i in range(0, len(words), chunk_size):
                        chunk_words = words[i:i + chunk_size]
                        chunk_content = " ".join(chunk_words)
                        if i > 0:
                            chunk_content = " " + chunk_content
                        
                        chunk = {
                            "id": f"resp_{uuid.uuid4().hex[:24]}",
                            "object": "response.delta",
                            "created": int(time.time()),
                            "model": internal_request.get("original_model", "gpt-3.5-turbo"),
                            "delta": {"content": chunk_content}
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        await asyncio.sleep(0.1)
                    
                    # Final chunk
                    final_chunk = {
                        "id": f"resp_{uuid.uuid4().hex[:24]}",
                        "object": "response.completed",
                        "created": int(time.time()),
                        "model": internal_request.get("original_model", "gpt-3.5-turbo"),
                        "status": "completed"
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                
                elif api_format == "anthropic":
                    # Anthropic streaming format
                    words = actual_response.split()
                    chunk_size = max(1, len(words) // 8)
                    
                    for i in range(0, len(words), chunk_size):
                        chunk_words = words[i:i + chunk_size]
                        chunk_content = " ".join(chunk_words)
                        if i > 0:
                            chunk_content = " " + chunk_content
                        
                        chunk = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": chunk_content}
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        await asyncio.sleep(0.1)
                    
                    # Final chunk
                    final_chunk = {
                        "type": "message_stop"
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                
                elif api_format in ["ollama_generate", "ollama_chat"]:
                    # Ollama streaming format (no SSE, just JSON lines)
                    words = actual_response.split()
                    chunk_size = max(1, len(words) // 10)  # More chunks for Ollama
                    
                    for i in range(0, len(words), chunk_size):
                        chunk_words = words[i:i + chunk_size]
                        chunk_content = " ".join(chunk_words)
                        if i > 0:
                            chunk_content = " " + chunk_content
                        
                        if api_format == "ollama_generate":
                            chunk = {
                                "model": internal_request.get("original_model", "llama2"),
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                "response": chunk_content,
                                "done": False
                            }
                        else:  # ollama_chat
                            chunk = {
                                "model": internal_request.get("original_model", "llama2"),
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
                                "message": {
                                    "role": "assistant",
                                    "content": chunk_content
                                },
                                "done": False
                            }
                        
                        yield json.dumps(chunk) + "\n"
                        await asyncio.sleep(0.05)  # Faster streaming for Ollama
                    
                    # Final chunk
                    if api_format == "ollama_generate":
                        final_chunk = {
                            "model": internal_request.get("original_model", "llama2"),
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
                    else:  # ollama_chat
                        final_chunk = {
                            "model": internal_request.get("original_model", "llama2"),
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
                
                else:
                    # For other formats, just return the response
                    response_data = {
                        "response": actual_response,
                        "original_model": internal_request.get("original_model", "default")
                    }
                    formatted_response = self._convert_from_internal_format(response_data, api_format)
                    yield f"data: {json.dumps(formatted_response)}\n\n"
                    yield "data: [DONE]\n\n"
            else:
                yield f"data: {json.dumps({'error': 'Timeout waiting for response'})}\n\n"
                yield "data: [DONE]\n\n"
                
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            self.metrics.record_request(
                internal_request.get("model", "default") if 'internal_request' in locals() else "unknown",
                time.time() - start_time,
                success
            )

# ─────────────────────────── FastAPI App ────────────────────────────────────

def create_enhanced_app(config: Dict[str, Any]) -> FastAPI:
    """Create FastAPI app with configuration"""
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Enhanced proxy server starting up...")
        yield
        logger.info("Enhanced proxy server shutting down...")
    
    app = FastAPI(
        title="Enhanced Universal Inference Proxy",
        description="Production-ready proxy for AI inference services",
        version="2.0.0",
        lifespan=lifespan
    )
    
    # Add CORS middleware
    cors_origins = config.get("cors_origins") or [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8080"
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize proxy server
    proxy = ProxyServer(config)

    # ─────────────── API Key Authentication ─────────────────

    auth_config = config.get("authentication", {})
    require_api_key = auth_config.get("require_api_key", False)
    configured_api_key = auth_config.get("api_key")

    async def verify_api_key(request: Request):
        if not require_api_key:
            return True
        if not configured_api_key:
            raise HTTPException(status_code=503, detail="API key required but not configured")
        provided = request.headers.get("x-api-key")
        if not provided:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
        if not provided or not hmac.compare_digest(provided.encode("utf-8"), configured_api_key.encode("utf-8")):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return True

    # ─────────────── OpenAI API Routes ─────────────────
    
    @app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
    async def openai_chat_completions(request: OpenAIChatRequest):
        """OpenAI Chat Completions API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        
        # Check if streaming is requested
        if request_dict.get("stream", False):
            return StreamingResponse(
                proxy.handle_streaming_request(request_dict, "openai"),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            return await proxy.handle_request(request_dict, "openai")
    
    @app.post("/v1/responses", dependencies=[Depends(verify_api_key)])
    async def openai_responses(request: OpenAIResponseRequest):
        """OpenAI Responses API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        
        # Check if streaming is requested
        if request_dict.get("stream", False):
            return StreamingResponse(
                proxy.handle_streaming_request(request_dict, "openai_responses"),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            return await proxy.handle_request(request_dict, "openai_responses")
    
    # ─────────────── Anthropic API Routes ─────────────────
    
    @app.post("/v1/messages", dependencies=[Depends(verify_api_key)])
    async def anthropic_messages(request: AnthropicRequest):
        """Anthropic Messages API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        
        # Check if streaming is requested
        if request_dict.get("stream", False):
            return StreamingResponse(
                proxy.handle_streaming_request(request_dict, "anthropic"),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            return await proxy.handle_request(request_dict, "anthropic")
    
    # ─────────────── Ollama API Routes ─────────────────
    
    @app.post("/api/generate", dependencies=[Depends(verify_api_key)])
    async def ollama_generate(request: OllamaGenerateRequest):
        """Ollama Generate API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        
        # Ollama typically defaults to streaming
        if request_dict.get("stream", True):
            return StreamingResponse(
                proxy.handle_streaming_request(request_dict, "ollama_generate"),
                media_type="application/json",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            return await proxy.handle_request(request_dict, "ollama_generate")
    
    @app.post("/api/chat", dependencies=[Depends(verify_api_key)])
    async def ollama_chat(request: OllamaChatRequest):
        """Ollama Chat API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        
        # Ollama typically defaults to streaming
        if request_dict.get("stream", True):
            return StreamingResponse(
                proxy.handle_streaming_request(request_dict, "ollama_chat"),
                media_type="application/json",
                headers={"Cache-Control": "no-cache"}
            )
        else:
            return await proxy.handle_request(request_dict, "ollama_chat")
    
    # ─────────────── Task-Specific HTTP Passthrough Routes ─────────────────
    
    @app.api_route("/{task_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"], dependencies=[Depends(verify_api_key)])
    async def task_specific_http_passthrough(request: Request, task_id: str, path: str):
        """Task-specific HTTP passthrough to backend services"""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise HTTPException(status_code=400, detail="Invalid task_id format")
        if not proxy.channels:
            raise HTTPException(status_code=503, detail="No backends available")
        
        # Find channel for specific task or use first available
        channel = proxy.channels[0]  # In multi-task setup, you'd route by task_id
        
        try:
            # Forward the request to the specific task
            result = await channel.forward_http_request(request, path, task_id)
            
            if result.get("success"):
                return Response(
                    content=result.get("content", ""),
                    status_code=result.get("status_code", 200),
                    headers=result.get("headers", {}),
                    media_type=result.get("media_type", "application/json")
                )
            else:
                raise HTTPException(
                    status_code=result.get("status_code", 500),
                    detail=result.get("error", "Backend request failed")
                )
                
        except Exception as e:
            logger.error(f"Task HTTP passthrough error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"], dependencies=[Depends(verify_api_key)])
    async def generic_http_passthrough(request: Request, path: str):
        """Generic HTTP passthrough to backend services (uses default task)"""
        if not proxy.channels:
            raise HTTPException(status_code=503, detail="No backends available")
        
        # Use first available channel
        channel = proxy.channels[0]
        
        try:
            # Forward the request to the backend
            result = await channel.forward_http_request(request, path)
            
            if result.get("success"):
                return Response(
                    content=result.get("content", ""),
                    status_code=result.get("status_code", 200),
                    headers=result.get("headers", {}),
                    media_type=result.get("media_type", "application/json")
                )
            else:
                raise HTTPException(
                    status_code=result.get("status_code", 500),
                    detail=result.get("error", "Backend request failed")
                )
                
        except Exception as e:
            logger.error(f"HTTP passthrough error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/api/generate-generic", dependencies=[Depends(verify_api_key)])
    async def generic_generate(request: GenericRequest):
        """Generic/Passthrough API"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        return await proxy.handle_request(request_dict, "generic")
    
    @app.post("/api/v1/generate", dependencies=[Depends(verify_api_key)])
    async def generic_generate_v1(request: GenericRequest):
        """Generic API v1 (alternative endpoint)"""
        request_dict = request.model_dump()  # Fixed Pydantic v2 deprecation
        return await proxy.handle_request(request_dict, "generic")
    
    # ─────────────── Health & Status Routes ─────────────────
    
    @app.get("/health")
    async def health_check(auth: bool = Depends(proxy.auth.optional_verify)):
        """Health check with optional JWT authentication"""
        return JSONResponse({
            "status": "healthy",
            "timestamp": time.time(),
            "uptime_seconds": proxy.metrics.uptime_seconds,
            "backends": len(proxy.channels),
            "service": "enhanced-inference-proxy"
        })
    
    @app.get("/status")
    async def get_status():
        """Detailed status information"""
        return JSONResponse({
            "service": {
                "name": "enhanced-inference-proxy",
                "version": "2.0.0",
                "status": "running"
            },
            "metrics": {
                "uptime_seconds": proxy.metrics.uptime_seconds,
                "total_requests": proxy.metrics.request_count,
                "error_rate": proxy.metrics.error_rate,
                "avg_response_time": proxy.metrics.avg_response_time,
                "cache_hit_ratio": proxy.metrics.cache_hit_ratio
            },
            "backends": len(proxy.channels),
            "available_models": proxy.available_models,
            "model_mapping": {
                "enabled": proxy.model_mapping_config.get("enabled", True),
                "mappings": proxy.model_mapping_config.get("mappings", {})
            },
            "supported_apis": {
                "openai": {
                    "endpoints": ["/v1/chat/completions", "/v1/responses"],
                    "streaming": True
                },
                "anthropic": {
                    "endpoints": ["/v1/messages"],
                    "streaming": True
                },
                "ollama": {
                    "endpoints": ["/api/generate", "/api/chat"],
                    "streaming": True
                },
                "generic": {
                    "endpoints": ["/api/generate-generic", "/api/v1/generate"],
                    "streaming": False
                },
                "http_proxy": {
                    "endpoints": ["/{task_id}/proxy/{path}", "/proxy/{path}"],
                    "streaming": False,
                    "description": "Task-specific and generic HTTP passthrough with automatic response decryption"
                }
            }
        })
    
    @app.get("/metrics")
    async def get_metrics():
        """Prometheus-style metrics"""
        metrics_text = f"""# HELP proxy_requests_total Total number of requests
# TYPE proxy_requests_total counter
proxy_requests_total {proxy.metrics.request_count}

# HELP proxy_errors_total Total number of errors
# TYPE proxy_errors_total counter
proxy_errors_total {proxy.metrics.error_count}

# HELP proxy_response_time_seconds Average response time
# TYPE proxy_response_time_seconds gauge
proxy_response_time_seconds {proxy.metrics.avg_response_time}

# HELP proxy_cache_hit_ratio Cache hit ratio
# TYPE proxy_cache_hit_ratio gauge
proxy_cache_hit_ratio {proxy.metrics.cache_hit_ratio}

# HELP proxy_uptime_seconds Server uptime
# TYPE proxy_uptime_seconds counter
proxy_uptime_seconds {proxy.metrics.uptime_seconds}

# HELP proxy_backends_total Number of configured backends
# TYPE proxy_backends_total gauge
proxy_backends_total {len(proxy.channels)}

# HELP proxy_available_models Number of available models
# TYPE proxy_available_models gauge
proxy_available_models {len(proxy.available_models)}
"""
        return Response(content=metrics_text, media_type="text/plain")
    
    # ─────────────── Admin Routes ─────────────────
    
    @app.post("/admin/cache/clear")
    async def clear_cache():
        """Clear the response cache"""
        proxy.cache.clear()
        return {"status": "cache_cleared"}
    
    @app.get("/admin/models")
    async def list_models():
        """List all available and mapped models"""
        return {
            "available_models": proxy.available_models,
            "model_mapping": {
                "enabled": proxy.model_mapping_config.get("enabled", True),
                "mappings": proxy.model_mapping_config.get("mappings", {})
            },
            "supported_formats": ["openai", "openai_responses", "anthropic", "ollama_generate", "ollama_chat", "generic"]
        }
    
    @app.get("/admin/apis")
    async def list_apis():
        """List all supported API endpoints"""
        return {
            "openai": {
                "chat_completions": {
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "streaming": True,
                    "description": "OpenAI Chat Completions API"
                },
                "responses": {
                    "method": "POST", 
                    "path": "/v1/responses",
                    "streaming": True,
                    "description": "OpenAI Responses API"
                }
            },
            "anthropic": {
                "messages": {
                    "method": "POST",
                    "path": "/v1/messages", 
                    "streaming": True,
                    "description": "Anthropic Messages API"
                }
            },
            "ollama": {
                "generate": {
                    "method": "POST",
                    "path": "/api/generate",
                    "streaming": True,
                    "description": "Ollama Generate API"
                },
                "chat": {
                    "method": "POST",
                    "path": "/api/chat",
                    "streaming": True,
                    "description": "Ollama Chat API"
                }
            },
            "generic": {
                "generate": {
                    "method": "POST",
                    "path": "/api/generate-generic",
                    "streaming": False,
                    "description": "Generic/Passthrough API"
                },
                "generate_v1": {
                    "method": "POST",
                    "path": "/api/v1/generate",
                    "streaming": False,
                    "description": "Generic API v1"
                }
            }
        }
    
    return app

# ─────────────────────────── Main Function ──────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enhanced Universal Inference Proxy")
    parser.add_argument("--config", help="Configuration file path (YAML or JSON)")
    parser.add_argument("--port", type=int, default=8002, help="Server port")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    
    # Quick setup options (override config file)
    parser.add_argument("--backend", help="Backend URL")
    parser.add_argument("--api-key", help="Required API key for authentication")
    parser.add_argument("--aes-key", help="AES key file path for encryption")
    
    # NEW: Response handling options
    parser.add_argument("--no-decrypt", action="store_true", 
                       help="Disable automatic decryption of AES-encrypted responses")
    parser.add_argument("--preserve-headers", action="store_true",
                       help="Preserve all original response headers")
    parser.add_argument("--no-force-json", action="store_true",
                       help="Don't automatically set content-type to application/json for JSON responses")
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load configuration
    if args.config:
        config = load_config_from_file(args.config)
        logger.info(f"Loaded configuration from {args.config}")
    else:
        config = create_default_config()
        logger.info("Using default configuration")
    
    # Apply command line overrides
    if args.backend:
        config["backends"] = [{
            "id": "backend1",
            "url": args.backend,
            "weight": 1
        }]
        logger.info(f"Using backend: {args.backend}")
    
    if args.api_key:
        config["authentication"]["require_api_key"] = True
        config["authentication"]["api_key"] = args.api_key
        logger.info("API key authentication enabled")
    
    if args.aes_key:
        try:
            aes_key_file = Path(args.aes_key)
            if not aes_key_file.exists():
                logger.error(f"AES key file not found: {args.aes_key}")
                return 1
            
            raw = aes_key_file.read_bytes().strip()
            try:
                config["aes_key"] = base64.b64decode(raw)
                logger.info("AES encryption enabled")
            except Exception as e:
                logger.error(f"Failed to decode AES key: {e}")
                return 1
        except Exception as e:
            logger.error(f"Failed to load AES key file: {e}")
            return 1
    
    # NEW: Apply response handling overrides
    if not config.get("response_handling"):
        config["response_handling"] = {}
    
    if args.no_decrypt:
        config["response_handling"]["decrypt_responses"] = False
        logger.info("Response decryption disabled via command line")
    
    if args.preserve_headers:
        config["response_handling"]["preserve_original_headers"] = True
        logger.info("Preserving original response headers")
    
    if args.no_force_json:
        config["response_handling"]["force_json_content_type"] = False
        logger.info("Automatic JSON content-type detection disabled")
    
    # Validate configuration
    if not config.get("backends"):
        logger.error("No backends configured")
        return 1
    
    # Create and run app
    app = create_enhanced_app(config)
    
    logger.info("="*60)
    logger.info("🚀 Enhanced Universal Inference Proxy")
    logger.info("="*60)
    logger.info(f"📡 Server: {args.host}:{args.port}")
    logger.info(f"🔧 Backends: {len(config['backends'])}")
    logger.info(f"🗺️  Model Mapping: {'Enabled' if config.get('model_mapping', {}).get('enabled', True) else 'Disabled'}")
    logger.info(f"📋 Available Models: {len(config.get('available_models', []))}")
    logger.info(f"🔒 JWT Auth: {'Enabled' if config.get('authentication', {}).get('jwt', {}).get('enabled') else 'Disabled'}")
    logger.info(f"🛡️  AES Encryption: {'Enabled' if config.get('aes_key') else 'Disabled'}")
    
    # NEW: Log response handling configuration
    response_config = config.get("response_handling", {})
    logger.info(f"🔓 Response Decryption: {'Enabled' if response_config.get('decrypt_responses', True) else 'Disabled'}")
    logger.info(f"📄 Force JSON Content-Type: {'Enabled' if response_config.get('force_json_content_type', True) else 'Disabled'}")
    logger.info(f"📋 Preserve Headers: {'Enabled' if response_config.get('preserve_original_headers', False) else 'Disabled'}")
    
    logger.info(f"💾 Cache: {config.get('cache_max_size')} entries, {config.get('cache_ttl_seconds')}s TTL")
    logger.info("="*60)
    logger.info("🌐 Supported APIs:")
    logger.info("   • OpenAI Chat: POST /v1/chat/completions")
    logger.info("   • OpenAI Responses: POST /v1/responses")
    logger.info("   • Anthropic: POST /v1/messages")
    logger.info("   • Ollama Generate: POST /api/generate")
    logger.info("   • Ollama Chat: POST /api/chat")
    logger.info("   • Generic: POST /api/generate-generic")
    logger.info("   • Task HTTP Proxy: ANY /{task_id}/proxy/{path}")
    logger.info("   • Generic HTTP Proxy: ANY /proxy/{path}")
    logger.info("📊 Management:")
    logger.info("   • Health: GET /health")
    logger.info("   • Status: GET /status")
    logger.info("   • Metrics: GET /metrics")
    logger.info("   • Models: GET /admin/models")
    logger.info("   • APIs: GET /admin/apis")
    logger.info("="*60)
    
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
