#!/usr/bin/env python3
"""LZ4 Pixi Runner Server – full, clean build with JSON structured logging.
Enhanced with generic HTTP proxy support for any backend service.
FIXED: MCP input processing and loop prevention
NEW: PHASE 1 OFFLINE DEPLOYMENT with .pixi folder detection and handling
NEW: JSON structured logging with rotation and secure log retrieval

Run examples
------------
# local (open, no auth)
pixi run python rixi_server.py --port 9000

# with JWT auth
pixi run python rixi_server.py --public-key jwt_pub.pem --log-level INFO

# production: AES + JWT, debug logging
pixi run python rixi_server.py --public-key jwt_pub.pem --aes-key aes.key --log-level DEBUG --log-size-mb 5

# generate a random AES key
pixi run python rixi_server.py --gen-aes
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import logging.handlers
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional, List
from datetime import datetime

import jwt
import lz4.frame
import requests
import uvicorn
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from fastapi import Body
from fastapi import Request

class HandshakeReq(BaseModel):
    secret: str
    rotate: bool | None = False

# ═══════════════════════════════════════════════════════════════════════════
# NEW: JSON STRUCTURED LOGGING SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging"""
    
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add extra context if available
        if hasattr(record, 'task_id'):
            log_entry["task_id"] = record.task_id
        if hasattr(record, 'deployment_type'):
            log_entry["deployment_type"] = record.deployment_type
        if hasattr(record, 'request_id'):
            log_entry["request_id"] = record.request_id
        if hasattr(record, 'user_id'):
            log_entry["user_id"] = record.user_id
        if hasattr(record, 'endpoint'):
            log_entry["endpoint"] = record.endpoint
        if hasattr(record, 'method'):
            log_entry["method"] = record.method
        if hasattr(record, 'status_code'):
            log_entry["status_code"] = record.status_code
        if hasattr(record, 'duration_ms'):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, 'error_code'):
            log_entry["error_code"] = record.error_code
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_entry)

class LoggingConfig:
    """Centralized logging configuration"""
    
    def __init__(self, log_level: str = "INFO", log_dir: str = "logs", 
                 max_size_mb: int = 10, backup_count: int = 10):
        self.log_level = getattr(logging, log_level.upper())
        self.log_dir = pathlib.Path(log_dir)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.backup_count = backup_count
        
        # Create logs directory
        self.log_dir.mkdir(exist_ok=True)
        
        # Setup rotating file handler
        self.log_file = self.log_dir / "pixi_runner.log"
        self.handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=self.max_size_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        
        # Set JSON formatter
        self.handler.setFormatter(JSONFormatter())
        
        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(self.log_level)
        root_logger.addHandler(self.handler)
        
        # Also log to console for debugging (but only WARNING and above to avoid clutter)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.WARNING)  # Only show warnings/errors on console
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        root_logger.addHandler(console_handler)
        
        # Reduce noise from external libraries
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("requests.packages.urllib3").setLevel(logging.WARNING)
        
        # NEW: Track task-specific loggers and handlers
        self.task_loggers = {}  # task_id -> logger
        self.task_handlers = {}  # task_id -> file handler
        
    def create_task_logger(self, task_id: str) -> logging.Logger:
        """Create a dedicated logger for a specific task"""
        if task_id in self.task_loggers:
            return self.task_loggers[task_id]
        
        # Create task-specific log file
        task_log_file = self.log_dir / f"task_{task_id}.log"
        
        # Create task-specific logger
        task_logger = logging.getLogger(f"task.{task_id}")
        task_logger.setLevel(self.log_level)
        
        # Create task-specific file handler
        task_handler = logging.handlers.RotatingFileHandler(
            task_log_file,
            maxBytes=self.max_size_bytes,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        
        # Set JSON formatter for task logs
        task_handler.setFormatter(JSONFormatter())
        
        # Add handler to logger
        task_logger.addHandler(task_handler)
        
        # Store references
        self.task_loggers[task_id] = task_logger
        self.task_handlers[task_id] = task_handler
        
        logger.info("Task logger created", extra={"task_id": task_id, "log_file": str(task_log_file)})
        return task_logger
    
    def cleanup_task_logger(self, task_id: str):
        """Clean up task-specific logger when task finishes"""
        if task_id in self.task_loggers:
            task_logger = self.task_loggers[task_id]
            task_handler = self.task_handlers[task_id]
            
            # Close and remove handler
            task_handler.close()
            task_logger.removeHandler(task_handler)
            
            # Remove from tracking
            del self.task_loggers[task_id]
            del self.task_handlers[task_id]
            
            logger.info("Task logger cleaned up", extra={"task_id": task_id})
    
    def get_task_logger(self, task_id: str) -> Optional[logging.Logger]:
        """Get existing task logger if it exists"""
        return self.task_loggers.get(task_id)

    def get_log_files(self) -> List[str]:
        """Get list of available log files"""
        log_files = []
        # Current log file
        if self.log_file.exists():
            log_files.append(self.log_file.name)
        
        # Rotated log files
        for i in range(1, self.backup_count + 1):
            rotated_file = self.log_dir / f"{self.log_file.name}.{i}"
            if rotated_file.exists():
                log_files.append(rotated_file.name)
        
        # Task-specific log files
        for log_file in self.log_dir.glob("task_*.log*"):
            if log_file.is_file():
                log_files.append(log_file.name)
        
        return sorted(log_files, reverse=True)  # Most recent first

# Global logging config
logging_config: Optional[LoggingConfig] = None
logger = logging.getLogger(__name__)

def setup_logging(log_level: str = "INFO", log_dir: str = "logs", 
                  max_size_mb: int = 10, backup_count: int = 10):
    """Initialize logging system"""
    global logging_config
    logging_config = LoggingConfig(log_level, log_dir, max_size_mb, backup_count)
    logger.info("JSON structured logging initialized", extra={
        "log_level": log_level,
        "log_dir": log_dir,
        "max_size_mb": max_size_mb,
        "backup_count": backup_count
    })

# ─────────────────────────── Global auth state ────────────────────────────
class AuthSettings:
    def __init__(self):
        self.enabled: bool = False
        self.public_key: Optional[str] = None
        self.public_key_path: Optional[str] = None
        self.jwks_url: Optional[str] = None
        self.jwks_keys: Dict[str, Dict[str, Any]] = {}

auth_settings = AuthSettings()
security = HTTPBearer(auto_error=False)
secret_uses_remaining: int | None = None   # None = unlimited

aes_key: Optional[bytes] = None  # AES‑256 key

# ─────────────────────────── AES helpers ──────────────────────────────────
NONCE_LEN = 12
# at top
handshake_secret: str | None = None          # set at startup if provided
ephemeral_privkey = None                     # RSA private key for current handshake

def setup_handshake(secret_arg: str | None, max_uses: int):
    global handshake_secret, secret_uses_remaining
    if secret_arg:
        handshake_secret      = secret_arg
        secret_uses_remaining = None if max_uses == 0 else max_uses
        uses_txt = "unlimited" if secret_uses_remaining is None else secret_uses_remaining
        logger.info("Handshake secret enabled", extra={"max_uses": uses_txt})
        print(f"Handshake secret enabled (max uses = {uses_txt})")

# after your existing encrypt()/decrypt()
def send_frame(data: bytes) -> bytes:
    enc = encrypt(data)              # nonce|cipher
    return len(enc).to_bytes(4, "big") + enc

def encrypt(data: bytes) -> bytes:
    if not aes_key:
        return data
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(aes_key).encrypt(nonce, data, None)

def decrypt(data: bytes) -> bytes:
    if not aes_key:
        return data
    return AESGCM(aes_key).decrypt(data[:NONCE_LEN], data[NONCE_LEN:], None)

# ─────────────────────────── FastAPI app / lifespan ───────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Server starting up", extra={
        "auth_enabled": auth_settings.enabled,
        "public_key_path": auth_settings.public_key_path,
        "jwks_url": auth_settings.jwks_url,
        "aes_encryption": aes_key is not None
    })
    print(
        f"Startup – auth_enabled={auth_settings.enabled}, "
        f"public_key_path={auth_settings.public_key_path}, jwks_url={auth_settings.jwks_url}"
    )
    if auth_settings.jwks_url:
        await refresh_jwks_keys()
    yield
    logger.info("Server shutting down")
    print("Shutdown complete")

def create_app() -> FastAPI:
    setup_auth(os.getenv("PUBLIC_KEY_PATH"), os.getenv("JWKS_URL"))
    return FastAPI(title="LZ4 Pixi Runner with HTTP Proxy + Offline Deployment + Logging", lifespan=lifespan)

app = FastAPI(title="LZ4 Pixi Runner with HTTP Proxy + Offline Deployment + Logging", lifespan=lifespan)

# ─────────────────────────── JWT helpers ──────────────────────────────────

def _b64url_decode(val: str) -> bytes:
    return base64.urlsafe_b64decode(val + "=" * (-len(val) % 4))

async def refresh_jwks_keys() -> None:
    if not auth_settings.jwks_url:
        return
    try:
        resp = requests.get(auth_settings.jwks_url, timeout=5)
        resp.raise_for_status()
        auth_settings.jwks_keys = {k["kid"]: k for k in resp.json().get("keys", []) if "kid" in k}
        logger.info("JWKS keys refreshed", extra={"key_count": len(auth_settings.jwks_keys)})
        print(f"JWKS refreshed – {len(auth_settings.jwks_keys)} keys")
    except Exception as exc:
        logger.error("JWKS refresh failed", extra={"error": str(exc)})
        print("JWKS refresh error:", exc)

async def validate_token(token: str) -> tuple[bool, Optional[dict]]:
    """Validate JWT token and return (is_valid, decoded_payload)"""
    try:
        hdr = jwt.get_unverified_header(token)
        alg = hdr.get("alg", "RS256")
        kid = hdr.get("kid")

        # JWKS
        if auth_settings.jwks_keys and kid:
            key = auth_settings.jwks_keys.get(kid) or (await refresh_jwks_keys() or auth_settings.jwks_keys.get(kid))
            if not key or key.get("kty") != "RSA":
                return False, None
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.hazmat.backends import default_backend
            from cryptography.hazmat.primitives import serialization
            n = int.from_bytes(_b64url_decode(key["n"]), "big")
            e = int.from_bytes(_b64url_decode(key["e"]), "big")
            pem = rsa.RSAPublicNumbers(e, n).public_key(default_backend()).public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            payload = jwt.decode(token, pem, algorithms=[alg], options={"verify_aud": False})
            return True, payload

        # local PEM
        if auth_settings.public_key:
            try:
                payload = jwt.decode(token, auth_settings.public_key, algorithms=[alg], options={"verify_aud": False})
                return True, payload
            except jwt.InvalidAlgorithmError:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.backends import default_backend
                try:
                    key_obj = serialization.load_pem_public_key(auth_settings.public_key.encode(), backend=default_backend())
                    payload = jwt.decode(token, key_obj, algorithms=[alg], options={"verify_aud": False})
                    return True, payload
                except Exception:
                    return False, None
        return False, None
    except jwt.PyJWTError as exc:
        logger.warning("Token validation failed", extra={"error": str(exc)})
        print("Token validation error:", exc)
        return False, None

async def verify_authentication(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    if not auth_settings.enabled:
        return True
    bearer = authorization.credentials if authorization and authorization.scheme.lower() == "bearer" else None
    for tok in filter(None, (bearer,)):
        is_valid, payload = await validate_token(tok)
        if is_valid:
            return payload  # Return payload for use in log retrieval
    logger.warning("Authentication failed", extra={"endpoint": "verify_authentication"})
    raise HTTPException(status_code=401, detail="Invalid or missing authentication token.")

async def verify_logs_access(
    authorization: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Special authentication for log retrieval - requires logs_retrieval:enabled claim"""
    if not auth_settings.enabled:
        return True

    bearer = authorization.credentials if authorization and authorization.scheme.lower() == "bearer" else None
    for tok in filter(None, (bearer,)):
        is_valid, payload = await validate_token(tok)
        if is_valid and payload:
            # Check for logs_retrieval claim
            if payload.get("logs_retrieval") == "enabled":
                logger.info("Log access granted", extra={
                    "user_id": payload.get("sub", "unknown"),
                    "claims": list(payload.keys())
                })
                return payload
            else:
                logger.warning("Log access denied - missing logs_retrieval claim", extra={
                    "user_id": payload.get("sub", "unknown"),
                    "claims": list(payload.keys())
                })
                raise HTTPException(status_code=403, detail="Insufficient permissions for log access. Required claim: logs_retrieval=enabled")
    
    logger.warning("Log access denied - invalid token")
    raise HTTPException(status_code=401, detail="Invalid or missing authentication token.")

# ─────────────────────────── Auth setup utils ─────────────────────────────

def load_key(path: str) -> Optional[str]:
    try:
        key_content = pathlib.Path(path).read_text()
        logger.info("Public key loaded", extra={"key_path": path})
        return key_content
    except Exception as exc:
        logger.error("Failed to load public key", extra={"key_path": path, "error": str(exc)})
        print("Key load error:", exc)
        return None

def setup_auth(pub: Optional[str], jwks: Optional[str]) -> None:
    auth_settings.public_key_path = pub
    auth_settings.jwks_url = jwks
    if pub and (key := load_key(pub)):
        auth_settings.public_key = key
        auth_settings.enabled = True
        logger.info("Authentication enabled via local public key")
        print("Auth enabled via local public key")
    if jwks:
        auth_settings.enabled = True
        logger.info("Authentication enabled via JWKS URL", extra={"jwks_url": jwks})
        print("Auth enabled via JWKS URL")
    if not auth_settings.enabled:
        logger.warning("Authentication disabled - running in open mode")
        print("Authentication disabled – open mode")

# ─────────────────────────── Task helpers ─────────────────────────────────
running_tasks: Dict[str, Dict[str, Any]] = {}

def cleanup_task(tid: str) -> None:
    info = running_tasks.pop(tid, None)
    if not info:
        return
    proc: subprocess.Popen | None = info.get("process")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            logger.info("Task process terminated", extra={"task_id": tid})
        except Exception as e:
            logger.error("Failed to terminate task process", extra={"task_id": tid, "error": str(e)})
    temp_dir = info.get("temp_dir", "")
    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info("Task cleanup completed", extra={"task_id": tid, "temp_dir": temp_dir})
    
    # NEW: Clean up task-specific logger
    if logging_config:
        logging_config.cleanup_task_logger(tid)

# ─────────────────────────── HTTP Proxy Response Handler ───────────────────

class HTTPProxyResponseManager:
    """Manages HTTP proxy responses using task-based routing"""

    def __init__(self):
        self.pending_requests = {}  # task_id -> {request_id, event, timestamp}
        self.response_data = {}      # task_id -> response data

    def create_request_waiter(self, task_id: str, request_id: str) -> asyncio.Event:
        """Create an event to wait for response from a specific task"""
        event = asyncio.Event()
        self.pending_requests[task_id] = {
            "request_id": request_id,
            "event": event,
            "timestamp": time.time()
        }
        logger.debug("HTTP proxy request waiter created", extra={
            "task_id": task_id, 
            "request_id": request_id
        })
        return event

    def set_response_for_task(self, task_id: str, response_data: dict):
        """Set response data for a task and notify waiters"""
        self.response_data[task_id] = response_data
        if task_id in self.pending_requests:
            self.pending_requests[task_id]["event"].set()
            logger.debug("HTTP proxy response set", extra={"task_id": task_id})

    def get_response_for_task(self, task_id: str) -> Optional[dict]:
        """Get response data for a task if available"""
        return self.response_data.pop(task_id, None)

    def cleanup_task_request(self, task_id: str):
        """Clean up request resources for a task"""
        self.pending_requests.pop(task_id, None)
        self.response_data.pop(task_id, None)

http_proxy_manager = HTTPProxyResponseManager()

# ─────────────────────────── MCP Response Manager ─────────────────────────

class MCPResponseManager:
    """Manages MCP responses to prevent infinite loops"""
    
    def __init__(self):
        self.processed_requests = {}  # task_id -> set of processed request_ids
        self.request_timestamps = {}  # task_id -> {request_id: timestamp}
        
    def is_request_processed(self, task_id: str, request_id: str) -> bool:
        """Check if a request was already processed"""
        if task_id not in self.processed_requests:
            self.processed_requests[task_id] = set()
            self.request_timestamps[task_id] = {}
        
        return request_id in self.processed_requests[task_id]
    
    def mark_request_processed(self, task_id: str, request_id: str):
        """Mark a request as processed"""
        if task_id not in self.processed_requests:
            self.processed_requests[task_id] = set()
            self.request_timestamps[task_id] = {}
        
        self.processed_requests[task_id].add(request_id)
        self.request_timestamps[task_id][request_id] = time.time()
        
        logger.debug("MCP request marked as processed", extra={
            "task_id": task_id,
            "request_id": request_id
        })
        
        # Clean up old requests (older than 5 minutes)
        current_time = time.time()
        old_requests = []
        for req_id, timestamp in self.request_timestamps[task_id].items():
            if current_time - timestamp > 300:  # 5 minutes
                old_requests.append(req_id)
        
        for req_id in old_requests:
            self.processed_requests[task_id].discard(req_id)
            self.request_timestamps[task_id].pop(req_id, None)
    
    def cleanup_task(self, task_id: str):
        """Clean up all data for a task"""
        self.processed_requests.pop(task_id, None)
        self.request_timestamps.pop(task_id, None)
        logger.debug("MCP task data cleaned up", extra={"task_id": task_id})

mcp_response_manager = MCPResponseManager()

# ═══════════════════════════════════════════════════════════════════════════════════════════════
# NEW: OFFLINE DEPLOYMENT SUPPORT (PHASE 1)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def _detect_package_type(tar_path: str) -> dict:
    """Detect if package is offline (contains .pixi folder) and extract metadata"""
    package_info = {
        "is_offline": False,
        "pixi_lock_hash": None,
        "metadata": None
    }
    
    try:
        with tarfile.open(tar_path, "r") as tar:
            # Check for offline metadata file
            try:
                metadata_member = tar.getmember(".offline_metadata.json")
                metadata_file = tar.extractfile(metadata_member)
                if metadata_file:
                    metadata = json.loads(metadata_file.read().decode('utf-8'))
                    package_info["metadata"] = metadata
                    package_info["is_offline"] = metadata.get("package_type") == "offline"
                    package_info["pixi_lock_hash"] = metadata.get("pixi_lock_hash")
                    logger.info("Offline package metadata detected", extra={
                        "package_type": metadata.get("package_type"),
                        "stats": metadata.get("stats", {})
                    })
                    print(f"📋 Offline package metadata found: {metadata.get('stats', {})}")
                    return package_info
            except KeyError:
                pass  # No metadata file, check for .pixi folder directly
            
            # Fallback: check for .pixi folder in tar contents
            for member in tar.getmembers():
                if member.name.startswith(".pixi/") and member.isfile():
                    package_info["is_offline"] = True
                    logger.info("Offline package detected (legacy detection)")
                    print("🔍 Offline package detected (legacy detection - no metadata)")
                    break
                    
    except Exception as e:
        logger.error("Error detecting package type", extra={"error": str(e)})
        print(f"⚠️ Error detecting package type: {e}")
    
    return package_info

def _get_folder_size(folder_path: str) -> int:
    """Calculate total size of folder in bytes"""
    total_size = 0
    try:
        for root, dirs, files in os.walk(folder_path):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    total_size += os.path.getsize(fp)
    except Exception as e:
        logger.error("Error calculating folder size", extra={"folder": folder_path, "error": str(e)})
        print(f"⚠️ Error calculating folder size: {e}")
    return total_size

# ─────────────────────────── ENHANCED Pixi runner generator (with offline support) ──────────────────
async def extract_and_run(pkg: str, task: str, tid: Optional[str]) -> AsyncGenerator[bytes, None]:
    tmp = tempfile.mkdtemp()
    if tid:
        running_tasks[tid] = {
            "temp_dir": tmp,
            "task_name": task,
            "status": "extracting",
            "started_at": time.time(),
            "process": None,
            "output_lines": [],
            "reader_ready": threading.Event(),
            "offline_package": False,  # NEW: Track package type
            "pixi_lock_hash": None,    # NEW: For future caching (Phase 2)
        }
        
        # NEW: Create task-specific logger
        if logging_config:
            task_logger = logging_config.create_task_logger(tid)
            running_tasks[tid]["task_logger"] = task_logger
    
    logger.info("Extracting package", extra={"task_id": tid, "temp_dir": tmp})
    print(f"📦 Extracting package to {tmp}")
    yield send_frame((json.dumps({"status": f"Extracting to {tmp}", "task_id": tid}) + "\n").encode())

    tar_path = os.path.join(tmp, "package.tar")
    with lz4.frame.open(pkg, "rb") as src, open(tar_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    
    # NEW: Check if this is an offline package before extraction
    package_info = _detect_package_type(tar_path)
    is_offline = package_info.get("is_offline", False)
    pixi_lock_hash = package_info.get("pixi_lock_hash")
    metadata = package_info.get("metadata")
    
    if tid and tid in running_tasks:
        running_tasks[tid]["offline_package"] = is_offline
        running_tasks[tid]["pixi_lock_hash"] = pixi_lock_hash
        if metadata:
            running_tasks[tid]["package_metadata"] = metadata
    
    # Extract package (includes .pixi folder if present)
    with tarfile.open(tar_path) as tar:
        tar.extractall(tmp)
    os.remove(tar_path)
    
    # NEW: Enhanced logging and validation based on package type
    if is_offline:
        pixi_folder = os.path.join(tmp, ".pixi")
        if os.path.exists(pixi_folder):
            pixi_size = _get_folder_size(pixi_folder)
            pixi_size_mb = pixi_size / (1024 * 1024)
            logger.info("Offline package validated", extra={
                "task_id": tid,
                "deployment_type": "offline",
                "pixi_size_mb": round(pixi_size_mb, 1),
                "pixi_lock_hash": pixi_lock_hash[:16] + "..." if pixi_lock_hash else None
            })
            print(f"🔒 Offline package detected - dependencies included ({pixi_size_mb:.1f} MB)")
            
            # Validate .pixi folder structure
            essential_files = []
            for root, dirs, files in os.walk(pixi_folder):
                essential_files.extend([f for f in files if f.endswith(('.yaml', '.yml', '.toml', '.json'))])
            
            if len(essential_files) < 5:  # Basic validation
                logger.warning("Incomplete pixi folder detected", extra={
                    "task_id": tid,
                    "config_files_count": len(essential_files)
                })
                print(f"⚠️ Warning: .pixi folder may be incomplete ({len(essential_files)} config files found)")
            
            yield send_frame((json.dumps({
                "status": f"Offline package: {pixi_size_mb:.1f} MB dependencies included", 
                "task_id": tid,
                "deployment_type": "offline"
            }) + "\n").encode())
            
            if pixi_lock_hash:
                print(f"🔑 pixi.lock hash: {pixi_lock_hash[:16]}... (ready for Phase 2 caching)")
        else:
            logger.warning("Offline package claimed but .pixi folder not found - falling back to online", extra={"task_id": tid})
            print("⚠️ Offline package claimed but .pixi folder not found - treating as online")
            is_offline = False  # Fallback to online behavior
            if tid and tid in running_tasks:
                running_tasks[tid]["offline_package"] = False
    else:
        logger.info("Online package detected", extra={"task_id": tid, "deployment_type": "online"})
        print("🌐 Online package - dependencies will be downloaded")
        yield send_frame((json.dumps({
            "status": "Online package: dependencies will be downloaded", 
            "task_id": tid,
            "deployment_type": "online"
        }) + "\n").encode())

    if tid:
        running_tasks[tid]["status"] = "starting"

    cmd = f"cd {tmp} && pixi run {task}"
    
    # ULTIMATE FIX: Use script to capture ALL command output including shell setup
    # This wrapper script captures even the initial pixi command output AND the program output
    task_log_file = os.path.join(tmp, "task_output.log")
    script_content = f'''#!/bin/bash
set -o pipefail
cd "{tmp}"
# Set environment variables to force unbuffered output from Python programs
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
# Force pixi to be verbose and unbuffered
export PIXI_LOG_LEVEL=info
# Use stdbuf to disable buffering and capture ALL output including pixi's own logs
stdbuf -oL -eL pixi run --verbose {task} 2>&1 | tee "{task_log_file}"
exit ${{PIPESTATUS[0]}}
'''
    
    script_path = os.path.join(tmp, "run_wrapper.sh")
    with open(script_path, 'w') as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)
    
    # Enhanced process startup with offline mode logging
    if is_offline:
        logger.info("Starting task with offline dependencies", extra={"task_id": tid, "deployment_type": "offline"})
        print(f"🚀 Starting task with offline dependencies (no internet required)")
        yield send_frame((json.dumps({
            "status": "Starting task with offline dependencies", 
            "task_id": tid
        }) + "\n").encode())
    else:
        logger.info("Starting task - downloading dependencies", extra={"task_id": tid, "deployment_type": "online"})
        print(f"🚀 Starting task - pixi will download dependencies")
        yield send_frame((json.dumps({
            "status": "Starting task - downloading dependencies", 
            "task_id": tid
        }) + "\n").encode())
    
    # ENHANCED: Set environment to ensure pixi uses local dependencies if available
    env = {
        **os.environ,
        "PIXI_NO_COLOR": "1"  # Disable colors for cleaner output
    }
    
    # Add explicit cache configuration for offline mode
    if is_offline:
        # Point pixi to use the local .pixi folder
        env["PIXI_CACHE_DIR"] = os.path.join(tmp, ".pixi")
        env["PIXI_OFFLINE"] = "1"  # Hint to pixi that we're in offline mode
    
    # FIXED: Run the wrapper script to capture ALL output from the very beginning
    proc = subprocess.Popen(
        ["/bin/bash", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # CRITICAL: Merge stderr into stdout to catch ALL logs
        stdin=subprocess.PIPE,
        text=True,
        bufsize=0,  # Unbuffered to catch output immediately
        preexec_fn=os.setsid,
        env=env  # Use enhanced environment
    )

    if tid:
        running_tasks[tid]["process"] = proc
        running_tasks[tid]["status"] = "running"
        logger.info("Task process started", extra={
            "task_id": tid,
            "pid": proc.pid,
            "deployment_type": "offline" if is_offline else "online"
        })

    # FIXED: Enhanced output reader that captures ALL output from the very beginning
    def reader(stream, name):
        try:
            # Signal that reader is ready
            if tid and tid in running_tasks:
                running_tasks[tid]["reader_ready"].set()
            
            for ln in iter(stream.readline, ""):
                if not ln:  # EOF
                    break
                    
                if tid in running_tasks:
                    running_tasks[tid]["output_lines"].append((name, ln))
                    
                    # IMMEDIATE: Print to console for real-time viewing
                    deployment_type = "OFFLINE" if running_tasks[tid].get("offline_package") else "ONLINE"
                    task_logger = running_tasks[tid].get("task_logger")
                    
                    if name == "output":
                        print(f"[{tid[:8] if tid else 'LIVE'}|{deployment_type}] {ln.rstrip()}")
                        # Log stdout to structured logs (files only)
                        logger.info("Task stdout", extra={
                            "task_id": tid,
                            "deployment_type": deployment_type.lower(),
                            "output_type": "stdout",
                            "content": ln.rstrip()
                        })
                        # NEW: Also log to task-specific log file
                        if task_logger:
                            task_logger.info(f"Task output: {ln.rstrip()}", extra={
                                "task_id": tid,
                                "deployment_type": deployment_type.lower(),
                                "output_type": "stdout",
                                "content": ln.rstrip(),
                                "timestamp": time.time()
                            })
                    elif name == "stderr":
                        print(f"[{tid[:8] if tid else 'LIVE'}|{deployment_type}] ERR: {ln.rstrip()}")
                        # Log stderr to structured logs (files only, but this will show on console due to WARNING level)
                        logger.warning("Task stderr", extra={
                            "task_id": tid,
                            "deployment_type": deployment_type.lower(),
                            "output_type": "stderr", 
                            "content": ln.rstrip()
                        })
                        # NEW: Also log to task-specific log file
                        if task_logger:
                            task_logger.warning(f"Task error: {ln.rstrip()}", extra={
                                "task_id": tid,
                                "deployment_type": deployment_type.lower(),
                                "output_type": "stderr",
                                "content": ln.rstrip(),
                                "timestamp": time.time()
                            })

                    # Check for HTTP proxy responses - TASK-BASED ROUTING
                    if name == "output" and ln.strip():
                        try:
                            data = json.loads(ln.strip())
                            if data.get("type") == "http_response":
                                logger.debug("HTTP response detected", extra={"task_id": tid})
                                print(f"🔄 Detected HTTP response for task {tid}")
                                http_proxy_manager.set_response_for_task(tid, data)
                        except (json.JSONDecodeError, KeyError):
                            pass
        except Exception as e:
            logger.error("Output reader error", extra={"task_id": tid, "stream": name, "error": str(e)})
            if tid:
                print(f"Reader error for {name}: {e}")

    # FIXED: Start reader threads BEFORE we start yielding output
    stdout_thread = threading.Thread(target=reader, args=(proc.stdout, "output"), daemon=True)
    stdout_thread.start()
    
    # FIXED: Wait a moment for readers to be ready and capture early output
    if tid and tid in running_tasks:
        running_tasks[tid]["reader_ready"].wait(timeout=0.5)
    
    # Small delay to ensure initial startup logs are captured
    await asyncio.sleep(0.2)

    idx = 0
    while proc.poll() is None:
        info = running_tasks.get(tid)
        lines = info["output_lines"] if info else []
        while idx < len(lines):
            lt, lc = lines[idx]; idx += 1
            yield send_frame((json.dumps({lt: lc.rstrip("\n") + "\n", "task_id": tid}) + "\n").encode())

        if not info:
            return  # task deleted mid-stream
        await asyncio.sleep(0.1)

    # FIXED: Ensure we capture any final output after process ends
    # Wait a bit for final output to be captured
    await asyncio.sleep(0.2)

    # flush remaining lines
    info = running_tasks.get(tid)
    lines = info["output_lines"] if info else []
    while idx < len(lines):
        lt, lc = lines[idx]; idx += 1
        yield send_frame((json.dumps({lt: lc.rstrip("\n") + "\n", "task_id": tid}) + "\n").encode())

    status = "completed" if proc.returncode == 0 else "failed"
    if info:
        info["status"] = status
        info["exit_code"] = proc.returncode
    
    # Enhanced completion logging
    deployment_type = "offline" if info and info.get("offline_package") else "online"
    logger.info("Task completed", extra={
        "task_id": tid,
        "status": status,
        "exit_code": proc.returncode,
        "deployment_type": deployment_type
    })
    print(f"🏁 Task {status} ({deployment_type} deployment)")
    
    # NEW: Capture and log the complete task output from the log file
    if info and tid:
        temp_dir = info.get("temp_dir")
        if temp_dir:
            task_log_file = os.path.join(temp_dir, "task_output.log")
            try:
                if os.path.exists(task_log_file):
                    with open(task_log_file, 'r') as f:
                        complete_output = f.read()
                    
                    # Log the complete program output to structured logs
                    logger.info("Complete task output captured", extra={
                        "task_id": tid,
                        "deployment_type": deployment_type,
                        "output_type": "complete_program_output",
                        "content": complete_output,
                        "content_length": len(complete_output)
                    })
                    
                    # Also log to task-specific log file if available
                    task_logger = info.get("task_logger")
                    if task_logger:
                        task_logger.info("Complete program output", extra={
                            "task_id": tid,
                            "deployment_type": deployment_type,
                            "output_type": "complete_program_output", 
                            "content": complete_output,
                            "content_length": len(complete_output),
                            "timestamp": time.time()
                        })
                    
                    print(f"📄 Captured {len(complete_output)} characters of program output for task {tid}")
                else:
                    logger.warning("Task output log file not found", extra={
                        "task_id": tid,
                        "expected_path": task_log_file
                    })
            except Exception as e:
                logger.error("Failed to read task output log", extra={
                    "task_id": tid,
                    "log_file": task_log_file,
                    "error": str(e)
                })
    
    yield send_frame((json.dumps({
        "status": f"Process {status}", 
        "exit_code": proc.returncode, 
        "task_id": tid,
        "deployment_type": deployment_type
    }) + "\n").encode())

    if not tid:
        shutil.rmtree(tmp)

# ═══════════════════════════════════════════════════════════════════════════
# NEW: LOG RETRIEVAL ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/logs")
async def list_log_files(auth: dict = Depends(verify_logs_access)):
    """List available log files - requires logs_retrieval:enabled JWT claim"""
    if not logging_config:
        return JSONResponse({"error": "Logging not configured"}, status_code=500)
    
    log_files = logging_config.get_log_files()
    file_info = []
    
    for filename in log_files:
        file_path = logging_config.log_dir / filename
        if file_path.exists():
            stat = file_path.stat()
            file_info.append({
                "filename": filename,
                "size_bytes": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "is_current": filename == logging_config.log_file.name
            })
    
    logger.info("Log files listed", extra={
        "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system",
        "file_count": len(file_info)
    })
    
    return JSONResponse({
        "log_files": file_info,
        "log_config": {
            "max_size_mb": logging_config.max_size_bytes / (1024 * 1024),
            "backup_count": logging_config.backup_count,
            "log_level": logging.getLevelName(logging_config.log_level)
        }
    })

@app.get("/logs/{filename}")
async def download_log_file(
    filename: str, 
    auth: dict = Depends(verify_logs_access),
    lines: Optional[int] = Query(None, description="Number of recent lines to return (tail)")
):
    """Download a specific log file - requires logs_retrieval:enabled JWT claim"""
    if not logging_config:
        return JSONResponse({"error": "Logging not configured"}, status_code=500)
    
    # Validate filename to prevent directory traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        logger.warning("Invalid log filename requested", extra={
            "filename": filename,
            "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system"
        })
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    
    file_path = logging_config.log_dir / filename
    if not file_path.exists():
        return JSONResponse({"error": "Log file not found"}, status_code=404)
    
    logger.info("Log file accessed", extra={
        "filename": filename,
        "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system",
        "lines_requested": lines
    })
    
    if lines:
        # Return last N lines
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()
                recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                content = ''.join(recent_lines)
            
            return Response(
                content=content,
                media_type="application/json",
                headers={
                    "Content-Disposition": f"attachment; filename={filename}.tail-{lines}.jsonl"
                }
            )
        except Exception as e:
            logger.error("Error reading log file", extra={"file_name": filename, "error": str(e)})
            return JSONResponse({"error": "Failed to read log file"}, status_code=500)
    else:
        # Return entire file
        return FileResponse(
            file_path,
            media_type="application/json",
            filename=filename
        )

@app.get("/logs/stream")
async def stream_logs(
    auth: dict = Depends(verify_logs_access),
    level: Optional[str] = Query("INFO", description="Minimum log level to stream"),
    follow: bool = Query(True, description="Follow new log entries")
):
    """Stream live logs - requires logs_retrieval:enabled JWT claim"""
    if not logging_config:
        return JSONResponse({"error": "Logging not configured"}, status_code=500)
    
    min_level = getattr(logging, level.upper(), logging.INFO)
    
    logger.info("Log streaming started", extra={
        "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system",
        "min_level": level,
        "follow": follow
    })
    
    async def generate_log_stream():
        try:
            file_path = logging_config.log_file
            
            # Send existing content first
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        try:
                            log_entry = json.loads(line.strip())
                            entry_level = getattr(logging, log_entry.get("level", "INFO"), logging.INFO)
                            if entry_level >= min_level:
                                yield f"data: {line}\n\n"
                        except json.JSONDecodeError:
                            continue
            
            if not follow:
                return
            
            # Follow new entries
            last_size = file_path.stat().st_size if file_path.exists() else 0
            
            while True:
                await asyncio.sleep(1)  # Check every second
                
                if not file_path.exists():
                    continue
                
                current_size = file_path.stat().st_size
                if current_size > last_size:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        f.seek(last_size)
                        for line in f:
                            try:
                                log_entry = json.loads(line.strip())
                                entry_level = getattr(logging, log_entry.get("level", "INFO"), logging.INFO)
                                if entry_level >= min_level:
                                    yield f"data: {line}\n\n"
                            except json.JSONDecodeError:
                                continue
                    last_size = current_size
                elif current_size < last_size:
                    # Log file was rotated
                    last_size = 0
                    
        except Exception as e:
            logger.error("Log streaming error", extra={"error": str(e)})
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return StreamingResponse(
        generate_log_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

@app.get("/logs/tasks")
async def download_all_task_logs(
    auth: dict = Depends(verify_logs_access),
    lines: Optional[int] = Query(None, description="Number of recent lines to return from each task log (tail)")
):
    """Download all task-specific log files - requires logs_retrieval:enabled JWT claim"""
    if not logging_config:
        return JSONResponse({"error": "Logging not configured"}, status_code=500)
    
    logger.info("All task logs requested", extra={
        "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system",
        "lines_requested": lines
    })
    
    # Find all task log files
    task_log_files = list(logging_config.log_dir.glob("task_*.log*"))
    
    if not task_log_files:
        return JSONResponse({"error": "No task log files found"}, status_code=404)
    
    # Create a combined response with all task logs
    combined_logs = {}
    
    for log_file in task_log_files:
        try:
            if lines:
                # Return last N lines for each task
                with open(log_file, 'r', encoding='utf-8') as f:
                    all_lines = f.readlines()
                    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    combined_logs[log_file.name] = ''.join(recent_lines)
            else:
                # Return entire file content
                with open(log_file, 'r', encoding='utf-8') as f:
                    combined_logs[log_file.name] = f.read()
        except Exception as e:
            logger.error("Error reading task log file", extra={
                "file_name": log_file.name, 
                "error": str(e)
            })
            combined_logs[log_file.name] = f"Error reading file: {str(e)}"
    
    filename_suffix = f".tail-{lines}" if lines else ""
    
    return Response(
        content=json.dumps(combined_logs, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=all_task_logs{filename_suffix}.json"
        }
    )

@app.get("/logs/task/{task_id}")
async def download_task_log(
    task_id: str,
    auth: dict = Depends(verify_logs_access),
    lines: Optional[int] = Query(None, description="Number of recent lines to return (tail)")
):
    """Download logs for a specific task ID - requires logs_retrieval:enabled JWT claim"""
    if not logging_config:
        return JSONResponse({"error": "Logging not configured"}, status_code=500)
    
    # Validate task_id to prevent directory traversal
    if ".." in task_id or "/" in task_id or "\\" in task_id:
        logger.warning("Invalid task ID requested", extra={
            "task_id": task_id,
            "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system"
        })
        return JSONResponse({"error": "Invalid task ID"}, status_code=400)
    
    # Look for task log file (including rotated versions)
    task_log_files = list(logging_config.log_dir.glob(f"task_{task_id}.log*"))
    
    if not task_log_files:
        return JSONResponse({"error": f"No log files found for task ID: {task_id}"}, status_code=404)
    
    logger.info("Task log accessed", extra={
        "task_id": task_id,
        "user_id": auth.get("sub", "unknown") if isinstance(auth, dict) else "system",
        "lines_requested": lines,
        "files_found": len(task_log_files)
    })
    
    # If multiple files (rotated logs), combine them in chronological order
    if len(task_log_files) > 1:
        # Sort by file extension (higher numbers are older)
        task_log_files.sort(key=lambda x: (
            int(x.suffix.split('.')[-1]) if x.suffix.count('.') > 1 else 0
        ), reverse=True)
        
        combined_content = ""
        for log_file in task_log_files:
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                    if file_content:
                        combined_content += f"# --- {log_file.name} ---\n{file_content}\n"
            except Exception as e:
                logger.error("Error reading task log file", extra={
                    "file_name": log_file.name,
                    "task_id": task_id, 
                    "error": str(e)
                })
                combined_content += f"# --- {log_file.name} (ERROR) ---\nError reading file: {str(e)}\n"
        
        if lines:
            # Apply line limit to combined content
            all_lines = combined_content.split('\n')
            recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
            content = '\n'.join(recent_lines)
        else:
            content = combined_content
        
        filename_suffix = f".tail-{lines}" if lines else ""
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=task_{task_id}_combined{filename_suffix}.jsonl"
            }
        )
    else:
        # Single file
        log_file = task_log_files[0]
        
        if lines:
            # Return last N lines
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    all_lines = f.readlines()
                    recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    content = ''.join(recent_lines)
                
                return Response(
                    content=content,
                    media_type="application/json",
                    headers={
                        "Content-Disposition": f"attachment; filename=task_{task_id}.tail-{lines}.jsonl"
                    }
                )
            except Exception as e:
                logger.error("Error reading task log file", extra={
                    "file_name": log_file.name,
                    "task_id": task_id,
                    "error": str(e)
                })
                return JSONResponse({"error": "Failed to read task log file"}, status_code=500)
        else:
            # Return entire file
            return FileResponse(
                log_file,
                media_type="application/json",
                filename=f"task_{task_id}.log"
            )

# ─────────────────────────── Routes (Enhanced with Logging) ────────────────

# Global request tracking
server_start_time = time.time()
server_request_count = 0

# Add request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    global server_request_count
    server_request_count += 1
    start_time = time.time()
    
    # Log request start
    logger.info("Request started", extra={
        "method": request.method,
        "endpoint": str(request.url.path),
        "client_ip": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown")
    })
    
    response = await call_next(request)
    
    # Log request completion
    duration_ms = round((time.time() - start_time) * 1000, 2)
    logger.info("Request completed", extra={
        "method": request.method,
        "endpoint": str(request.url.path),
        "status_code": response.status_code,
        "duration_ms": duration_ms
    })
    
    return response

@app.post("/upload")
async def upload(
    auth: bool = Depends(verify_authentication),
    file: UploadFile = File(...),
    task_name: str = Form("foobar"),
    keep_alive: str = Form("false"),
):
    keep = keep_alive.lower() == "true"
    tid = str(uuid.uuid4()) if keep else None
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".lz4")
    
    logger.info("Package upload started", extra={
        "task_id": tid,
        "task_name": task_name,
        "keep_alive": keep,
        "file_name": file.filename,
        "content_type": file.content_type
    })
    
    try:
        content = await file.read()
        temp.write(content)
        temp.close()
        
        logger.info("Package upload completed", extra={
            "task_id": tid,
            "file_size_bytes": len(content)
        })
        
        return StreamingResponse(
            extract_and_run(temp.name, task_name, tid),
            media_type="application/octet-stream" if aes_key else "application/json",
        )
    except Exception as exc:
        os.unlink(temp.name)
        logger.error("Package upload failed", extra={
            "task_id": tid,
            "error": str(exc)
        })
        raise HTTPException(status_code=500, detail=str(exc))

# ENHANCED: Task status endpoint with deployment type info
@app.get("/task/{tid}")
async def task_status(tid: str, auth: bool = Depends(verify_authentication)):
    info = running_tasks.get(tid)
    if not info:
        logger.warning("Task status requested for unknown task", extra={"task_id": tid})
        return JSONResponse({"error": "Task not found"}, status_code=404)
    
    proc = info.get("process")
    if proc and proc.poll() is not None and info["status"] == "running":
        info["status"] = "completed" if proc.returncode == 0 else "failed"
        info["exit_code"] = proc.returncode
    
    # Enhanced response with deployment info
    resp = {k: v for k, v in info.items() if k not in ["process", "reader_ready", "package_metadata", "task_logger"]}
    
    # Add deployment type information
    resp["deployment_type"] = "offline" if info.get("offline_package") else "online"
    if info.get("pixi_lock_hash"):
        resp["pixi_lock_hash"] = info["pixi_lock_hash"][:16] + "..."  # Show first 16 chars
    
    # Add package statistics if available
    if info.get("package_metadata"):
        metadata = info["package_metadata"]
        if "stats" in metadata:
            stats = metadata["stats"]
            resp["package_stats"] = {
                "code_files": stats.get("code_files", 0),
                "dependency_files": stats.get("dependency_files", 0),
                "pixi_size_mb": round(stats.get("pixi_size", 0) / (1024 * 1024), 1),
                "total_size_mb": round(stats.get("total_size", 0) / (1024 * 1024), 1)
            }
    
    if "output_lines" in resp:
        resp["recent_output"] = [{"type": t, "content": c.rstrip()} 
                               for t, c in resp.pop("output_lines")[-100:]]
    
    logger.debug("Task status retrieved", extra={"task_id": tid, "status": resp.get("status")})
    return JSONResponse(resp)

@app.delete("/task/{tid}")
async def terminate_task(tid: str, auth: bool = Depends(verify_authentication)):
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    
    logger.info("Task termination requested", extra={"task_id": tid})
    cleanup_task(tid)
    mcp_response_manager.cleanup_task(tid)  # Clean up MCP state
    return JSONResponse({"status": "terminated"})

# ─────────────────────────── ENHANCED Task Input Route (FIXED) ─────────────────────

@app.post("/task/{tid}/input")
async def send_input_to_task(
    tid: str,
    request: Request,
    auth: bool = Depends(verify_authentication),
):
    """Send input to a running task's stdin - ENHANCED with MCP support."""
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    info = running_tasks[tid]
    proc = info.get("process")

    if not proc or proc.poll() is not None:
        return JSONResponse({"error": "Task not running"}, status_code=400)

    try:
        # Get raw request body
        body = await request.body()
        content_type = request.headers.get("content-type", "")

        # Process based on content type and encryption
        if aes_key and "application/octet-stream" in content_type:
            try:
                # Decrypt the data if AES is enabled
                decrypted = decrypt(body)
                try:
                    # Try to parse as JSON after decryption
                    data_str = decrypted.decode('utf-8')
                    data = json.loads(data_str)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # If not valid JSON, use raw decrypted data
                    data = {"raw_data": str(decrypted)}
            except Exception as exc:
                return JSONResponse({"error": f"Decryption failed: {exc}"}, status_code=400)
        elif "application/json" in content_type:
            # No encryption - handle JSON data
            try:
                data = await request.json()
            except json.JSONDecodeError:
                return JSONResponse({"error": "Invalid JSON data"}, status_code=400)
        else:
            # Try to parse as plain text
            try:
                text = body.decode('utf-8')
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = {"raw_data": text}
            except UnicodeDecodeError:
                return JSONResponse({"error": "Unsupported content type"}, status_code=415)

        # ENHANCED: Handle MCP responses specially
        if isinstance(data, dict) and data.get("type") == "mcp_response":
            mcp_data = data.get("data", {})
            request_id = mcp_data.get("request_id")
            
            # Prevent processing the same MCP request multiple times
            if request_id and mcp_response_manager.is_request_processed(tid, request_id):
                logger.debug("Skipping already processed MCP request", extra={"task_id": tid, "request_id": request_id})
                print(f"🔄 Skipping already processed MCP request: {request_id}")
                return JSONResponse({"status": "already_processed", "request_id": request_id})
            
            # Mark as processed
            if request_id:
                mcp_response_manager.mark_request_processed(tid, request_id)
            
            deployment_type = "OFFLINE" if info.get("offline_package") else "ONLINE"
            logger.info("Sending MCP response to task", extra={
                "task_id": tid,
                "deployment_type": deployment_type,
                "action": mcp_data.get('action', 'unknown')
            })
            print(f"📤 Sending MCP response to task {tid} ({deployment_type}): {mcp_data.get('action', 'unknown')}")

        # Send to process stdin
        if proc.stdin:
            try:
                json_str = json.dumps(data) + "\n"
                proc.stdin.write(json_str)
                proc.stdin.flush()
                
                # ENHANCED: Better response for different input types  
                if isinstance(data, dict) and data.get("type") == "mcp_response":
                    return JSONResponse({
                        "status": "mcp_response_sent",
                        "request_id": data.get("data", {}).get("request_id"),
                        "action": data.get("data", {}).get("action", "unknown")
                    })
                else:
                    return JSONResponse({"status": "input_sent", "bytes": len(json_str)})
                    
            except BrokenPipeError:
                return JSONResponse({"error": "Process stdin closed"}, status_code=500)
            except Exception as e:
                return JSONResponse({"error": f"Failed to write to stdin: {str(e)}"}, status_code=500)
        else:
            return JSONResponse({"error": "Process stdin is not available"}, status_code=500)
            
    except Exception as exc:
        deployment_type = "OFFLINE" if info.get("offline_package") else "ONLINE"
        logger.error("Input processing error", extra={
            "task_id": tid,
            "deployment_type": deployment_type,
            "error": str(exc)
        })
        print(f"❌ Input processing error for task {tid} ({deployment_type}): {exc}")
        return JSONResponse({"error": f"Failed to send input: {exc}"}, status_code=500)

# ─────────────────────────── HTTP Proxy Route ─────────────────────

@app.api_route("/task/{tid}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def task_http_proxy(
    tid: str,
    path: str,
    request: Request,
    auth: bool = Depends(verify_authentication)
):
    """HTTP proxy endpoint for tasks - FIXED VERSION"""
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    info = running_tasks[tid]
    proc = info.get("process")

    if not proc or proc.poll() is not None:
        return JSONResponse({"error": "Task not running"}, status_code=400)

    try:
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        deployment_type = "OFFLINE" if info.get("offline_package") else "ONLINE"
        logger.info("HTTP proxy request", extra={
            "task_id": tid,
            "request_id": request_id,
            "deployment_type": deployment_type,
            "method": request.method,
            "path": path
        })
        print(f"🔗 Generating request ID: {request_id} for {deployment_type} task")

        # Get request data
        method = request.method.upper()
        query_string = str(request.url.query) if request.url.query else ""
        headers = dict(request.headers)

        # Prepare request data
        if method in ['POST', 'PUT', 'PATCH']:
            content_type = request.headers.get("content-type", "")

            if aes_key and "application/octet-stream" in content_type:
                body = await request.body()
                try:
                    decrypted_body = decrypt(body)
                    body_data = decrypted_body.decode('utf-8')
                    if "application/json" in content_type:
                        body_data = json.loads(body_data)
                except Exception as exc:
                    return JSONResponse({"error": f"Decryption failed: {exc}"}, status_code=400)
            else:
                if "application/json" in content_type:
                    body_data = await request.json()
                elif "application/x-www-form-urlencoded" in content_type:
                    body_data = dict(await request.form())
                else:
                    body_content = await request.body()
                    body_data = body_content.decode('utf-8') if body_content else ""
        else:
            body_data = None

        # Create HTTP request message
        http_request = {
            "type": "http_request",
            "request_id": request_id,
            "data": {
                "method": method,
                "path": path,
                "query": query_string,
                "headers": {
                    **{k: v for k, v in headers.items() if not k.lower().startswith(('content-length', 'host', 'connection'))},
                    "X-Request-ID": request_id  # Inject request_id as header
                },
                "body": body_data
            }
        }

        print(f"🔗 Sending HTTP request to task {tid} ({deployment_type}): {method} /{path}")

        # Send to process stdin
        if proc.stdin:
            # Create response waiter BEFORE sending request to avoid race condition
            response_event = http_proxy_manager.create_request_waiter(tid, request_id)
            
            print(f"📤 Sending HTTP request: {json.dumps(http_request)}")
            proc.stdin.write(f"{json.dumps(http_request)}\n")
            proc.stdin.flush()

            try:
                # Wait for response with timeout
                print(f"⏳ Waiting for response from task {tid} ({deployment_type})")
                await asyncio.wait_for(response_event.wait(), timeout=30.0)

                # Get response data
                response_data = http_proxy_manager.get_response_for_task(tid)
                if response_data and "data" in response_data:
                    logger.info("HTTP proxy response received", extra={
                        "task_id": tid,
                        "request_id": request_id,
                        "deployment_type": deployment_type
                    })
                    print(f"✅ Got HTTP response for task {tid} ({deployment_type})")
                    resp = response_data["data"]
                    response_content = resp.get("body", "")
                    response_headers = resp.get("headers", {})
                    status_code = resp.get("status", 200)

                    # Remove Content-Length header to avoid conflicts - FastAPI will calculate it
                    response_headers.pop("Content-Length", None)
                    response_headers.pop("content-length", None)

                    # Handle encryption if needed
                    if aes_key and isinstance(response_content, str):
                        encrypted_content = encrypt(response_content.encode('utf-8'))
                        return Response(
                            content=encrypted_content,
                            status_code=status_code,
                            headers={**response_headers, "Content-Type": "application/octet-stream"}
                        )
                    else:
                        # Determine content type
                        if isinstance(response_content, dict):
                            response_content = json.dumps(response_content)
                            if "Content-Type" not in response_headers:
                                response_headers["Content-Type"] = "application/json"

                        return Response(
                            content=response_content,
                            status_code=status_code,
                            headers=response_headers
                        )
                else:
                    logger.error("Invalid HTTP proxy response format", extra={
                        "task_id": tid,
                        "request_id": request_id,
                        "deployment_type": deployment_type
                    })
                    print(f"❌ Invalid response format for task {tid} ({deployment_type})")
                    return JSONResponse({"error": "Invalid response format from backend"}, status_code=502)

            except asyncio.TimeoutError:
                logger.warning("HTTP proxy request timeout", extra={
                    "task_id": tid,
                    "request_id": request_id,
                    "deployment_type": deployment_type
                })
                print(f"❌ HTTP request timeout for task {tid} ({deployment_type}), checking recent output...")
                # Fallback: manually check recent output for our response
                recent_lines = info.get("output_lines", [])[-50:]  # Check last 50 lines
                for line_type, content in recent_lines:
                    if line_type == "output" and content.strip():
                        try:
                            data = json.loads(content.strip())
                            if data.get("type") == "http_response":
                                print(f"✅ Found response in recent output for task {tid} ({deployment_type})")
                                resp = data["data"]
                                response_content = resp.get("body", "")
                                response_headers = resp.get("headers", {})
                                status_code = resp.get("status", 200)

                                # Remove Content-Length header to avoid conflicts - FastAPI will calculate it
                                response_headers.pop("Content-Length", None)
                                response_headers.pop("content-length", None)

                                # Handle encryption if needed
                                if aes_key and isinstance(response_content, str):
                                    encrypted_content = encrypt(response_content.encode('utf-8'))
                                    return Response(
                                        content=encrypted_content,
                                        status_code=status_code,
                                        headers={**response_headers, "Content-Type": "application/octet-stream"}
                                    )
                                else:
                                    # Determine content type
                                    if isinstance(response_content, dict):
                                        response_content = json.dumps(response_content)
                                        if "Content-Type" not in response_headers:
                                            response_headers["Content-Type"] = "application/json"

                                    return Response(
                                        content=response_content,
                                        status_code=status_code,
                                        headers=response_headers
                                    )
                        except (json.JSONDecodeError, KeyError):
                            continue
                
                print(f"❌ No response found in recent output for task {tid} ({deployment_type})")
                return JSONResponse({"error": "Backend response timeout"}, status_code=504)
            finally:
                http_proxy_manager.cleanup_task_request(tid)
        else:
            return JSONResponse({"error": "Process stdin not available"}, status_code=500)

    except Exception as exc:
        http_proxy_manager.cleanup_task_request(tid)
        deployment_type = "OFFLINE" if info.get("offline_package") else "ONLINE"
        logger.error("HTTP proxy error", extra={
            "task_id": tid,
            "deployment_type": deployment_type,
            "error": str(exc)
        })
        print(f"💥 HTTP proxy error for {deployment_type} task: {exc}")
        return JSONResponse({"error": f"Proxy error: {exc}"}, status_code=500)

# ─────────────────────────── Enhanced compatibility endpoints ──────────────

# ENHANCED: Health check with offline deployment capability
@app.get("/health")
async def health_check():
    offline_support_enabled = True  # Always enabled in this implementation
    
    return JSONResponse({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "uptime_seconds": time.time() - server_start_time,
        "service": "pixi-runner-server",
        "features": [
            "http_proxy", 
            "aes_encryption", 
            "jwt_auth", 
            "mcp_support",
            "offline_deployment",
            "json_logging"  # NEW feature
        ],
        "deployment_modes": {
            "online": True,
            "offline": offline_support_enabled
        },
        "logging": {
            "enabled": logging_config is not None,
            "level": logging.getLevelName(logging_config.log_level) if logging_config else "N/A"
        }
    })

# ENHANCED: Status endpoint with deployment statistics
@app.get("/status")
async def get_server_status():
    recent_output = []
    for tid, info in running_tasks.items():
        output_lines = info.get("output_lines", [])
        deployment_type = "offline" if info.get("offline_package") else "online"
        
        for line_type, content in output_lines[-10:]:
            recent_output.append({
                "type": "output",
                "content": json.dumps({
                    "task_id": tid,
                    "deployment_type": deployment_type,
                    "output_type": line_type,
                    "content": content.strip(),
                    "timestamp": datetime.utcnow().isoformat()
                })
            })

    return JSONResponse({
        "service": {"name": "pixi-runner-server", "status": "running"},
        "stats": {"total_requests": server_request_count, "active_tasks": len(running_tasks)},
        "recent_output": recent_output,
        "features": {
            "http_proxy": True,
            "aes_encryption": aes_key is not None,
            "jwt_auth": auth_settings.enabled,
            "mcp_support": True,
            "offline_deployment": True,
            "json_logging": logging_config is not None
        }
    })

# NEW: Deployment statistics endpoint
@app.get("/deployment/stats")
async def deployment_stats(auth: bool = Depends(verify_authentication)):
    """Get statistics about online vs offline deployments"""
    total_tasks = len(running_tasks)
    offline_tasks = sum(1 for info in running_tasks.values() if info.get("offline_package"))
    online_tasks = total_tasks - offline_tasks
    
    # Calculate package size statistics
    total_pixi_size = 0
    offline_task_details = []
    
    for tid, info in running_tasks.items():
        if info.get("offline_package") and info.get("package_metadata"):
            metadata = info["package_metadata"]
            if "stats" in metadata:
                stats = metadata["stats"]
                pixi_size = stats.get("pixi_size", 0)
                total_pixi_size += pixi_size
                
                offline_task_details.append({
                    "task_id": tid[:8] + "...",
                    "task_name": info.get("task_name", "unknown"),
                    "pixi_size_mb": round(pixi_size / (1024 * 1024), 1),
                    "started_at": info.get("started_at", 0)
                })
    
    return JSONResponse({
        "total_tasks": total_tasks,
        "online_deployments": online_tasks,
        "offline_deployments": offline_tasks,
        "offline_percentage": (offline_tasks / total_tasks * 100) if total_tasks > 0 else 0,
        "total_dependencies_size_mb": round(total_pixi_size / (1024 * 1024), 1),
        "offline_task_details": offline_task_details
    })

@app.get("/task/{tid}/health")
async def task_health_check(tid: str, auth: bool = Depends(verify_authentication)):
    info = running_tasks.get(tid)
    if not info:
        return JSONResponse({"task_id": tid, "status": "not_found", "can_accept_requests": False}, status_code=404)

    proc = info.get("process")
    is_running = proc and proc.poll() is None
    deployment_type = "offline" if info.get("offline_package") else "online"
    
    return JSONResponse({
        "task_id": tid,
        "status": "healthy" if is_running else "stopped",
        "can_accept_requests": is_running,
        "supports_http_proxy": True,
        "supports_mcp": True,
        "deployment_type": deployment_type
    })

# ─────────────────────────── Stream output ────────────────────────────────

@app.get("/task/{tid}/output")
async def get_task_output_history(tid: str, auth: bool = Depends(verify_authentication)):
    """Get the complete output history for a task (useful for debugging missing startup logs)"""
    info = running_tasks.get(tid)
    if not info:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    
    output_lines = info.get("output_lines", [])
    proc = info.get("process")
    deployment_type = "offline" if info.get("offline_package") else "online"
    
    # Format output for easy reading
    formatted_output = []
    for line_type, content in output_lines:
        formatted_output.append({
            "type": line_type,
            "content": content.rstrip(),
            "timestamp": time.time()  # This could be enhanced with actual timestamps
        })
    
    return JSONResponse({
        "task_id": tid,
        "status": info.get("status", "unknown"),
        "deployment_type": deployment_type,
        "total_lines": len(output_lines),
        "process_running": proc and proc.poll() is None if proc else False,
        "output_history": formatted_output,
        "recent_output": formatted_output[-20:] if len(formatted_output) > 20 else formatted_output
    })

@app.get("/task/{tid}/stream")
async def stream_task_output(tid: str, auth: bool = Depends(verify_authentication)):
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    async def _gen():
        idx = 0
        info = running_tasks.get(tid)
        if not info:
            return
            
        deployment_type = "offline" if info.get("offline_package") else "online"
        
        # ➊ ENHANCED backlog – send everything already captured with better logging
        lines = info.get("output_lines", [])
        logger.info("Stream connection established", extra={
            "task_id": tid,
            "deployment_type": deployment_type,
            "backlog_lines": len(lines)
        })
        print(f"📺 Stream connecting to task {tid} ({deployment_type.upper()}) - sending {len(lines)} backlog lines")
        
        for lt, lc in lines:
            frame_data = json.dumps({lt: lc.rstrip() + "\n", "task_id": tid, "deployment_type": deployment_type}) + "\n"
            yield send_frame(frame_data.encode())
            idx += 1
            
        print(f"📺 Backlog sent ({idx} lines), starting live stream for task {tid} ({deployment_type.upper()})")

        # ➋ enhanced live follow with better error handling
        while tid in running_tasks:
            info = running_tasks.get(tid)
            if not info:
                break
                
            lines = info.get("output_lines", [])
            
            # Send any new lines that appeared since last check
            while idx < len(lines):
                lt, lc = lines[idx]
                idx += 1
                frame_data = json.dumps({lt: lc.rstrip() + "\n", "task_id": tid, "deployment_type": deployment_type}) + "\n"
                yield send_frame(frame_data.encode())

            proc = info.get("process")
            if proc and proc.poll() is not None and idx >= len(lines):
                final_status = "completed" if proc.returncode == 0 else "failed"
                logger.info("Stream ending - task finished", extra={
                    "task_id": tid,
                    "deployment_type": deployment_type,
                    "status": final_status,
                    "exit_code": proc.returncode
                })
                print(f"📺 Task {tid} ({deployment_type.upper()}) {final_status} (exit code: {proc.returncode})")
                yield send_frame((json.dumps({
                    "status": f"Process {final_status}", 
                    "exit_code": proc.returncode, 
                    "task_id": tid,
                    "deployment_type": deployment_type
                }) + "\n").encode())
                break
                
            await asyncio.sleep(0.1)
            
        print(f"📺 Stream ended for task {tid} ({deployment_type.upper()})")

    return StreamingResponse(
        _gen(),
        media_type="application/octet-stream" if aes_key else "application/json",
    )

# ─────────────────────────── Task management ──────────────────────────────

@app.post("/task/{tid}/restart")
async def restart_task(
    tid: str,
    auth: bool = Depends(verify_authentication),
):
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    info = running_tasks[tid]
    temp_dir = info["temp_dir"]
    task_name = info["task_name"]
    deployment_type = "offline" if info.get("offline_package") else "online"

    logger.info("Task restart requested", extra={
        "task_id": tid,
        "deployment_type": deployment_type
    })

    # kill old process (if still running)
    proc = info.get("process")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass

    # Clean up MCP state for this task
    mcp_response_manager.cleanup_task(tid)

    # start new process with the same fix + offline mode support
    task_log_file = os.path.join(temp_dir, "task_output.log")
    script_content = f'''#!/bin/bash
set -o pipefail
cd "{temp_dir}"
# Set environment variables to force unbuffered output from Python programs
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
# Force pixi to be verbose and unbuffered
export PIXI_LOG_LEVEL=info
# Use stdbuf to disable buffering and capture ALL output including pixi's own logs
stdbuf -oL -eL pixi run --verbose {task_name} 2>&1 | tee "{task_log_file}"
exit ${{PIPESTATUS[0]}}
'''
    
    script_path = os.path.join(temp_dir, "restart_wrapper.sh")
    with open(script_path, 'w') as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)
    
    # Enhanced environment setup for restart
    env = {
        **os.environ,
        "PIXI_NO_COLOR": "1"
    }
    
    # Maintain offline mode settings
    if info.get("offline_package"):
        env["PIXI_CACHE_DIR"] = os.path.join(temp_dir, ".pixi")
        env["PIXI_OFFLINE"] = "1"
    
    proc = subprocess.Popen(
        ["/bin/bash", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout 
        stdin=subprocess.PIPE,
        text=True,
        bufsize=0,  # FIXED: Unbuffered
        preexec_fn=os.setsid,
        env=env  # Use enhanced environment
    )
    info.update(
        {"process": proc, "status": "running", "started_at": time.time(), "output_lines": []}
    )

    # async output readers with HTTP proxy support
    def reader(stream, typ):
        for ln in iter(stream.readline, ""):
            info["output_lines"].append((typ, ln))
            task_logger = info.get("task_logger")
            
            # Clean console output like servernew12.py  
            if typ == "output":
                print(f"[{tid[:8]}|{deployment_type.upper()}] {ln.rstrip()}")
                # Log to structured logs (files only)
                logger.info("Task stdout", extra={
                    "task_id": tid,
                    "deployment_type": deployment_type,
                    "output_type": "stdout",
                    "content": ln.rstrip()
                })
                # NEW: Also log to task-specific log file
                if task_logger:
                    task_logger.info(f"Task output: {ln.rstrip()}", extra={
                        "task_id": tid,
                        "deployment_type": deployment_type,
                        "output_type": "stdout",
                        "content": ln.rstrip(),
                        "timestamp": time.time()
                    })
            elif typ == "stderr":
                print(f"[{tid[:8]}|{deployment_type.upper()}] ERR: {ln.rstrip()}")
                # Log stderr to structured logs 
                logger.warning("Task stderr", extra={
                    "task_id": tid,
                    "deployment_type": deployment_type,
                    "output_type": "stderr",
                    "content": ln.rstrip()
                })
                # NEW: Also log to task-specific log file
                if task_logger:
                    task_logger.warning(f"Task error: {ln.rstrip()}", extra={
                        "task_id": tid,
                        "deployment_type": deployment_type,
                        "output_type": "stderr",
                        "content": ln.rstrip(),
                        "timestamp": time.time()
                    })

            # Check for HTTP proxy responses
            if typ == "output" and ln.strip():
                try:
                    data = json.loads(ln.strip())
                    if data.get("type") == "http_response":
                        print(f"🔄 [RESTART|{deployment_type.upper()}] Detected HTTP response for task {tid}")
                        http_proxy_manager.set_response_for_task(tid, data)
                except (json.JSONDecodeError, KeyError):
                    pass

    threading.Thread(target=reader, args=(proc.stdout, "output"), daemon=True).start()

    logger.info("Task restarted successfully", extra={
        "task_id": tid,
        "deployment_type": deployment_type
    })

    return JSONResponse({"status": "restarted", "task_id": tid, "deployment_type": deployment_type})

@app.post("/task/{tid}/redeploy")
async def redeploy_task(
    tid: str,
    auth: bool = Depends(verify_authentication),
    file: UploadFile = File(...),
):
    if tid not in running_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    info = running_tasks[tid]
    task_name = info["task_name"]
    old_deployment_type = "offline" if info.get("offline_package") else "online"

    logger.info("Task redeploy requested", extra={
        "task_id": tid,
        "old_deployment_type": old_deployment_type
    })

    # kill old process
    proc = info.get("process")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass

    # Clean up MCP state for this task
    mcp_response_manager.cleanup_task(tid)

    # save uploaded LZ4 package
    tmp_pkg = tempfile.NamedTemporaryFile(delete=False, suffix=".lz4")
    tmp_pkg.write(await file.read())
    tmp_pkg.close()

    # Detect new package type
    temp_tar = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    temp_tar.close()
    
    with lz4.frame.open(tmp_pkg.name, "rb") as src, open(temp_tar.name, "wb") as dst:
        dst.write(src.read())
    
    new_package_info = _detect_package_type(temp_tar.name)
    new_is_offline = new_package_info.get("is_offline", False)
    new_pixi_lock_hash = new_package_info.get("pixi_lock_hash")
    new_deployment_type = "offline" if new_is_offline else "online"
    
    print(f"🔄 Redeploying task {tid}: {old_deployment_type} → {new_deployment_type}")

    # new temp dir for code
    new_dir = tempfile.mkdtemp()
    tar_path = os.path.join(new_dir, "pkg.tar")
    shutil.move(temp_tar.name, tar_path)
    
    with tarfile.open(tar_path) as tar:
        tar.extractall(new_dir)
    os.unlink(tar_path)
    os.unlink(tmp_pkg.name)

    # update info with new package details
    info.update({
        "temp_dir": new_dir, 
        "output_lines": [],
        "offline_package": new_is_offline,
        "pixi_lock_hash": new_pixi_lock_hash
    })
    
    if new_package_info.get("metadata"):
        info["package_metadata"] = new_package_info["metadata"]

    # start new process with enhanced offline support
    task_log_file = os.path.join(new_dir, "task_output.log")
    script_content = f'''#!/bin/bash
set -o pipefail
cd "{new_dir}"
# Set environment variables to force unbuffered output from Python programs
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8
# Force pixi to be verbose and unbuffered
export PIXI_LOG_LEVEL=info
# Use stdbuf to disable buffering and capture ALL output including pixi's own logs
stdbuf -oL -eL pixi run --verbose {task_name} 2>&1 | tee "{task_log_file}"
exit ${{PIPESTATUS[0]}}
'''
    
    script_path = os.path.join(new_dir, "redeploy_wrapper.sh")
    with open(script_path, 'w') as f:
        f.write(script_content)
    os.chmod(script_path, 0o755)
    
    # Enhanced environment setup for redeploy
    env = {
        **os.environ,
        "PIXI_NO_COLOR": "1"
    }
    
    # Configure for offline mode if needed
    if new_is_offline:
        env["PIXI_CACHE_DIR"] = os.path.join(new_dir, ".pixi")
        env["PIXI_OFFLINE"] = "1"
    
    proc = subprocess.Popen(
        ["/bin/bash", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout
        stdin=subprocess.PIPE,
        text=True,
        bufsize=0,  # FIXED: Unbuffered
        preexec_fn=os.setsid,
        env=env  # Use enhanced environment
    )
    info.update(
        {"process": proc, "status": "running", "started_at": time.time()}
    )

    def reader(stream, typ):
        for ln in iter(stream.readline, ""):
            info["output_lines"].append((typ, ln))
            task_logger = info.get("task_logger")
            
            # Clean console output like servernew12.py
            if typ == "output":
                print(f"[{tid[:8]}|{new_deployment_type.upper()}] {ln.rstrip()}")
                # Log to structured logs (files only)
                logger.info("Task stdout", extra={
                    "task_id": tid,
                    "deployment_type": new_deployment_type,
                    "output_type": "stdout", 
                    "content": ln.rstrip()
                })
                # NEW: Also log to task-specific log file
                if task_logger:
                    task_logger.info(f"Task output: {ln.rstrip()}", extra={
                        "task_id": tid,
                        "deployment_type": new_deployment_type,
                        "output_type": "stdout",
                        "content": ln.rstrip(),
                        "timestamp": time.time()
                    })
            elif typ == "stderr":
                print(f"[{tid[:8]}|{new_deployment_type.upper()}] ERR: {ln.rstrip()}")
                # Log stderr to structured logs
                logger.warning("Task stderr", extra={
                    "task_id": tid,
                    "deployment_type": new_deployment_type,
                    "output_type": "stderr",
                    "content": ln.rstrip()
                })
                # NEW: Also log to task-specific log file
                if task_logger:
                    task_logger.warning(f"Task error: {ln.rstrip()}", extra={
                        "task_id": tid,
                        "deployment_type": new_deployment_type,
                        "output_type": "stderr",
                        "content": ln.rstrip(),
                        "timestamp": time.time()
                    })

            # Check for HTTP proxy responses
            if typ == "output" and ln.strip():
                try:
                    data = json.loads(ln.strip())
                    if data.get("type") == "http_response":
                        print(f"🔄 [REDEPLOY|{new_deployment_type.upper()}] Detected HTTP response for task {tid}")
                        http_proxy_manager.set_response_for_task(tid, data)
                except (json.JSONDecodeError, KeyError):
                    pass

    threading.Thread(target=reader, args=(proc.stdout, "output"), daemon=True).start()

    logger.info("Task redeployed successfully", extra={
        "task_id": tid,
        "old_deployment_type": old_deployment_type,
        "new_deployment_type": new_deployment_type
    })

    return JSONResponse({
        "status": "redeployed", 
        "task_id": tid, 
        "message": "New code running",
        "old_deployment_type": old_deployment_type,
        "new_deployment_type": new_deployment_type
    })

# ─────────────────────────── Handshake & Key Management ───────────────────

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

@app.post("/handshake")
async def handshake_step1(body: HandshakeReq,
                          auth: bool = Depends(verify_authentication)):
    """
    Step-1  Client → Server:
        JSON { "secret": <pre_shared>, "rotate": false|true }
    Response:
        401 if secret mismatch
        200 { "public_key": "<PEM>" }
    """
    global secret_uses_remaining
    # limit check
    if secret_uses_remaining is not None:
       if secret_uses_remaining <= 0:
          return JSONResponse({"error": "Secret usage limit reached"}, status_code=403)
       else:
          # consume one use
          secret_uses_remaining -= 1

    if handshake_secret is None:
        return JSONResponse({"error": "Handshake not configured"}, status_code=400)

    if body.secret != handshake_secret:
        return JSONResponse({"error": "Bad secret"}, status_code=401)

    if body.rotate:
        auth_settings.aes_key = None   # clear existing key
    # generate fresh RSA keypair
    global ephemeral_privkey
    ephemeral_privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = ephemeral_privkey.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    return {"public_key": pub_pem}

@app.post("/handshake/finish")
async def handshake_step2(req: Dict[str, str],
                          auth: bool = Depends(verify_authentication)):
    """
    Client sends:
        { "cipher": "<base64 RSA-encrypted blob>" }
        blob = AES_KEY(32B) || ROT_SECRET(32B)
    """
    if not ephemeral_privkey:
        return JSONResponse({"error": "No active handshake"}, status_code=400)

    import base64
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes

    ct = base64.b64decode(req["cipher"])
    try:
        plain = ephemeral_privkey.decrypt(
            ct,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        )
    except Exception as exc:
        return JSONResponse({"error": f"Decrypt failed: {exc}"}, status_code=400)
    global aes_key
    aes_key = plain[:32]
    rotation_secret = plain[32:64]

    # store both
    auth_settings.aes_key = aes_key
    auth_settings.rotation_secret = rotation_secret

    logger.info("New AES key registered via handshake")
    print("New AES key registered (via handshake)")
    return {"status": "ok"}

# ─────────────────────────── Task listing ──────────────────────

@app.get("/tasks")
async def list_tasks(auth: bool = Depends(verify_authentication)):
    """List all running tasks with deployment type info."""
    tasks = {}
    for tid, info in running_tasks.items():
        deployment_type = "offline" if info.get("offline_package") else "online"
        pixi_lock_hash = info.get("pixi_lock_hash")
        
        task_data = {
            "status": info.get("status", "unknown"),
            "task_name": info.get("task_name", ""),
            "started_at": info.get("started_at", 0),
            "supports_http_proxy": True,
            "supports_mcp": True,
            "deployment_type": deployment_type
        }
        
        if pixi_lock_hash:
            task_data["pixi_lock_hash"] = pixi_lock_hash[:16] + "..."
        
        # Add package size info for offline tasks
        if deployment_type == "offline" and info.get("package_metadata"):
            metadata = info["package_metadata"]
            if "stats" in metadata:
                stats = metadata["stats"]
                task_data["package_stats"] = {
                    "pixi_size_mb": round(stats.get("pixi_size", 0) / (1024 * 1024), 1),
                    "total_size_mb": round(stats.get("total_size", 0) / (1024 * 1024), 1)
                }
        
        tasks[tid] = task_data
    
    return JSONResponse(tasks)

# ─────────────────────────── AES key util & main ──────────────────────────

def generate_aes_key():
    key = os.urandom(32)
    print("# AES-256 Key (base64):")
    print(base64.b64encode(key).decode())

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Start LZ4 Pixi Runner with HTTP Proxy, MCP Support + Offline Deployment + JSON Logging")
    p.add_argument("--public-key")
    p.add_argument("--jwks-url")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--aes-key")
    p.add_argument("--gen-aes", action="store_true")
    p.add_argument("--no-reload", action="store_true")
    p.add_argument("--key-secret-uses", type=int, default=1,
                    help="How many times the secret can start a handshake "
                         "(1 = default, 0 = unlimited)")
    p.add_argument("--key-secret",
                    help="Pre-shared secret used for first AES key handshake")
    
    # NEW: Logging configuration arguments
    p.add_argument("--log-level", default="INFO", 
                    choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    help="Logging level (default: INFO)")
    p.add_argument("--log-dir", default="logs",
                    help="Directory for log files (default: logs)")
    p.add_argument("--log-size-mb", type=int, default=10,
                    help="Log file size limit in MB before rotation (default: 10)")
    p.add_argument("--log-backup-count", type=int, default=10,
                    help="Number of rotated log files to keep (default: 10)")

    args = p.parse_args()

    if args.gen_aes:
        generate_aes_key(); raise SystemExit

    # Initialize logging FIRST
    setup_logging(args.log_level, args.log_dir, args.log_size_mb, args.log_backup_count)

    if args.aes_key:
        aes_key = base64.b64decode(pathlib.Path(args.aes_key).read_text().strip())
        logger.info("AES key loaded from file")
        print("AES key loaded")

    setup_auth(args.public_key, args.jwks_url)
    setup_handshake(args.key_secret, args.key_secret_uses)

    print("="*70)
    print("🚀 Enhanced Pixi Runner Server with MCP + Offline Deployment + JSON Logging")
    print("="*70)
    print(f"📡 Server: 0.0.0.0:{args.port}")
    print(f"🔒 JWT Auth: {'Enabled' if auth_settings.enabled else 'Disabled'}")
    print(f"🛡️  AES Encryption: {'Enabled' if aes_key else 'Disabled'}")
    print(f"🌐 HTTP Proxy: Enabled")
    print(f"🔧 MCP Support: Enabled")
    print(f"📦 Offline Deployment: Enabled (Phase 1)")
    print(f"📝 JSON Logging: {args.log_level} → {args.log_dir}/ ({args.log_size_mb}MB chunks)")
    print("="*70)
    print("📋 Available Endpoints:")
    print("   • Upload: POST /upload")
    print("   • Task Status: GET /task/{id}")
    print("   • Task Input: POST /task/{id}/input (Enhanced with MCP)")
    print("   • HTTP Proxy: ANY /task/{id}/proxy/{path}")
    print("   • Stream: GET /task/{id}/stream")
    print("   • Health: GET /health")
    print("   • Tasks: GET /tasks")
    print("   • Deployment Stats: GET /deployment/stats")
    print("   🆕 Log Files: GET /logs (requires logs_retrieval:enabled)")
    print("   🆕 Download Log: GET /logs/{filename}")
    print("   🆕 Stream Logs: GET /logs/stream")
    print("   🆕 All Task Logs: GET /logs/tasks")
    print("   🆕 Task Log: GET /logs/task/{task_id}")
    print("="*70)
    print("🔒 Deployment Modes Supported:")
    print("   • Online: Dependencies downloaded on server (requires internet)")
    print("   • Offline: Dependencies packaged with code (air-gapped ready)")
    print("="*70)
    print("📝 Log Retrieval Security:")
    print("   • Requires JWT with 'logs_retrieval': 'enabled' claim")
    print("   • All log access attempts are logged")
    print("   • Structured JSON logging for better analysis")
    print("="*70)

    logger.info("Server startup initiated", extra={
        "port": args.port,
        "log_level": args.log_level,
        "auth_enabled": auth_settings.enabled,
        "aes_enabled": aes_key is not None
    })

    # Always serve the fully-configured module-level `app` (all routes/auth attach to it).
    # Hot-reload is intentionally not used: it needs an import-string app with per-worker
    # setup, which this single-module server doesn't provide. `--no-reload` is kept as an
    # accepted no-op for backward compatibility.
    uvicorn.run(app, host="0.0.0.0", port=args.port, reload=False)