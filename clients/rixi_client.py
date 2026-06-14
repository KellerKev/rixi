#!/usr/bin/env python3
"""
ENHANCED client.py - One-Step Deployment with MCP Back-Channel + HTTP(S) Reverse Proxy + Environment Injection + OFFLINE DEPLOYMENT + AUTO-APPROVE
Full feature parity with original client.py including:
- Signal handling with interactive menu
- Attach/detach capabilities  
- Restart/redeploy functionality
- Streaming output processing
- Complete encryption support
- Task management features
- FIXED AUTO-EXIT LOGIC
- FIXED Environment Variable Injection (file-based approach)
- FIXED Task ID handling (clear packaging vs server task ID)
- NEW: HTTP(S) Reverse Proxy for remote API access
- NEW: Config-based test server control
- NEW: PHASE 1 OFFLINE DEPLOYMENT with .pixi folder packaging
- NEW: AUTO-APPROVE functionality for confirmations
"""

import argparse
import asyncio
import json
import threading
import time
import uuid
import subprocess
import tempfile
import os
import shutil
import tarfile
import base64
import requests
import signal
import sys
import socket
import urllib.parse
import re
import hashlib
from collections import OrderedDict
from typing import Dict, List, Optional, Any, Union
from pathlib import Path

# Cryptography imports
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from rixi_transport import (
    Transport, NONCE_LEN, STREAM_TIMEOUT, _http, _http_session, _hdrs,
    _write_secret_file, DEFAULT_HEADERS_FILE, build_custom_headers,
    set_default_headers, mask_header_value,
)

# Optional aiohttp import for external MCP servers
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp not available - external MCP server routing will be disabled")
    print("💡 Install with: pip install aiohttp")

# Shared connection state used by the transport layer and signal handler
transport = Transport()


def load_pixi_config() -> Dict[str, Any]:
    """Load configuration from pixi_remote_config.toml"""
    config_file = Path("pixi_remote_config.toml")
    if not config_file.exists():
        return {}
    
    try:
        import tomli
        with open(config_file, 'rb') as f:
            config = tomli.load(f).get("config", {})
            
        # NEW: Support offline deployment config
        deployment_config = config.get("deployment", {})
        if deployment_config.get("offline_mode"):
            print("🔧 Configuration: Offline mode enabled by default")
        if deployment_config.get("auto_approve"):
            print("🔧 Configuration: Auto-approve enabled for confirmations")
            
        return config
    except ImportError:
        print("⚠️ tomli package not found. Install with: pip install tomli")
        return {}
    except Exception as e:
        print(f"⚠️ Could not load config: {e}")
        return {}


def _get_env_server_url() -> Optional[str]:
    """Return server URL from environment if set (first match)."""
    for var in ("PIXI_SERVER_URL", "PIXISERVER_URL", "SERVER_URL"):
        val = os.getenv(var)
        if val:
            return val.strip()
    return None


def _is_valid_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlparse(url)
        return parts.scheme in ("http", "https") and bool(parts.netloc)
    except Exception:
        return False


def _extract_server_from_token(token: str) -> Optional[tuple]:
    """Best-effort extraction of server URL from a JWT without verification.

    Checks common claim keys in order: server_url, server-url, aud, iss.
    For 'aud', supports string or list. Returns (url, claim) for the first
    valid http(s) URL found, or None.
    """
    if not token:
        return None
    # Some tokens may come prefixed like "Bearer <jwt>"; strip common prefixes
    t = token.strip()
    if t.lower().startswith("bearer "):
        t = t.split(None, 1)[1]
    try:
        try:
            import jwt  # pyjwt
        except ImportError:
            return None
        claims = jwt.decode(t, options={"verify_signature": False, "verify_aud": False})
    except Exception:
        return None

    # Candidate claim keys to inspect
    candidates: List[str] = [
        "server_url", "server-url", "aud", "iss"
    ]

    for key in candidates:
        if key not in claims:
            continue
        val = claims.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and _is_valid_url(item):
                    return item, key
        elif isinstance(val, str):
            if _is_valid_url(val):
                return val, key
    return None


def _resolve_server_url(cli_server: Optional[str], cfg: Dict[str, Any], cli_bearer: Optional[str],
                        allow_token_url: bool = False) -> (str, str):
    """Resolve server URL with precedence: CLI > env > config > token > default.

    Returns (url, source) where source is one of: 'CLI', 'env', 'config', 'token', 'default'.
    The 'token' source inspects the bearer JWT's claims for an embedded server URL and is
    only consulted when explicitly enabled via --server-from-token (allow_token_url=True).
    """
    # 1) CLI argument (explicit)
    if cli_server and cli_server.strip():
        return cli_server.strip(), "CLI"

    # 2) Environment variables
    env_url = _get_env_server_url()
    if env_url:
        return env_url, "env"

    # 3) Config file (support both server_url and server-url)
    cfg_url = cfg.get("server_url") or cfg.get("server-url")
    if isinstance(cfg_url, str) and cfg_url.strip():
        return cfg_url.strip(), "config"

    # 4) Bearer JWT claim (from CLI, env or config) - opt-in only
    if allow_token_url:
        token = (
            (cli_bearer or "").strip()
            or os.getenv("BEARER_TOKEN", "").strip()
            or os.getenv("PIXI_BEARER_TOKEN", "").strip()
            or os.getenv("AUTH_TOKEN", "").strip()
            or (cfg.get("bearer_token") or cfg.get("bearer-token") or "").strip()
        )
        token_result = _extract_server_from_token(token)
        if token_result:
            token_url, claim = token_result
            print("=" * 70)
            print(f"⚠️ SERVER URL DERIVED FROM BEARER TOKEN (--server-from-token)")
            print(f"   URL:   {token_url}")
            print(f"   Claim: '{claim}' (token signature NOT verified)")
            print("=" * 70)
            return token_url, f"token claim '{claim}'"

    # 5) Fallback default
    return "http://localhost:9000", "default"

def _quick_check_server_online(url: str, timeout: float = 1.5) -> (bool, str):
    """Quick TCP reachability check for the server host:port.

    Returns (ok, err). If ok is False, err contains a short reason.
    """
    try:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            return False, f"unsupported scheme: {parts.scheme or 'none'}"
        host = parts.hostname
        port = parts.port
        if not host:
            return False, "missing host"
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, ""
        except socket.gaierror as e:
            return False, f"DNS error: {e}"
        except ConnectionRefusedError:
            return False, "connection refused"
        except socket.timeout:
            return False, "connection timed out"
        except OSError as e:
            return False, f"OS error: {e.strerror or e}"
    except Exception as e:
        return False, str(e)



def _package_dir(path=".", env_handler=None, env_profile=None, offline_mode=False, auto_approve=False) -> str:
    """ENHANCED: Package directory with optional offline dependencies support + environment injection + auto-approve"""
    
    # Pre-flight checks for offline mode
    if offline_mode:
        if not _validate_offline_prerequisites(path, auto_approve):
            raise Exception("Offline mode validation failed")
    
    tar_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    tar_tmp.close()
    
    # Track what we're packaging
    package_stats = {
        "code_files": 0,
        "dependency_files": 0, 
        "total_size": 0,
        "pixi_size": 0
    }
    
    # FIXED: Create environment files in a temp dir BEFORE packaging if handler provided
    env_injection_enabled = bool(env_handler and env_handler.is_enabled())
    env_files_created = []
    env_tmp_dir = None
    if env_injection_enabled:
        try:
            env_vars = env_handler.get_environment_variables(env_profile)  # FIXED: Pass profile
            if env_vars:
                profile_info = f" (profile: {env_profile})" if env_profile else ""
                env_tmp_dir = tempfile.TemporaryDirectory()
                created_files = env_handler.create_env_files_for_packaging(env_vars, env_profile, env_tmp_dir.name)
                env_files_created = list(created_files.keys())
                print(f"🌍 Including {len(env_files_created)} environment files in package{profile_info}")
        except Exception as e:
            print(f"⚠️ Error creating environment files: {e}")

    try:
        with tarfile.open(tar_tmp.name, "w") as tar:
            for root, dirs, files in os.walk(path):
                # Enhanced directory filtering for offline mode
                if offline_mode:
                    # Include .pixi folder in offline mode, exclude other hidden dirs
                    dirs[:] = [d for d in dirs if not d.startswith(".") or d == ".pixi"]
                else:
                    # Original behavior - exclude all hidden dirs and __pycache__
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]

                for f in files:
                    # Enhanced file filtering logic
                    should_include = False
                    is_pixi_file = False
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, path)

                    # Include environment files only when env injection is enabled
                    # (generated files are added separately from the temp dir)
                    if f in [".env", ".env_injection.sh", ".env_vars.json"]:
                        should_include = env_injection_enabled and arcname not in env_files_created
                    # Include regular project files (not hidden, not compiled)
                    elif not f.startswith(".") and not f.endswith(".pyc") and not f.endswith("~"):
                        should_include = True
                    # In offline mode, include .pixi folder contents
                    elif offline_mode and ".pixi" in root:
                        should_include = True
                        is_pixi_file = True

                    if should_include:
                        # Get file size for stats
                        try:
                            file_size = os.path.getsize(fp)
                            package_stats["total_size"] += file_size

                            if is_pixi_file:
                                package_stats["dependency_files"] += 1
                                package_stats["pixi_size"] += file_size
                            else:
                                package_stats["code_files"] += 1

                            tar.add(fp, arcname)
                        except OSError as e:
                            print(f"⚠️ Warning: Could not add {fp}: {e}")

            # Add generated environment files from the temp dir with explicit arcnames
            if env_tmp_dir is not None:
                for arcname in env_files_created:
                    fp = os.path.join(env_tmp_dir.name, arcname)
                    try:
                        package_stats["total_size"] += os.path.getsize(fp)
                        package_stats["code_files"] += 1
                        tar.add(fp, arcname)
                    except OSError as e:
                        print(f"⚠️ Warning: Could not add {fp}: {e}")

        # Add offline package metadata
        if offline_mode:
            _add_offline_metadata(tar_tmp.name, package_stats, path)

        # Compress with LZ4
        lz4_path = tar_tmp.name + ".lz4"
        try:
            import lz4.frame
            with open(tar_tmp.name, "rb") as src, lz4.frame.open(lz4_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.unlink(tar_tmp.name)
            
            # Show package statistics
            _show_package_stats(package_stats, offline_mode, lz4_path)
            
            if env_files_created:
                print(f"📦 Package created with environment files: {', '.join(env_files_created)}")
            
            return lz4_path
        except ImportError:
            print("⚠️ LZ4 not available, using uncompressed tar")
            _show_package_stats(package_stats, offline_mode, tar_tmp.name)
            return tar_tmp.name
            
    finally:
        # Clean up temporary environment file directory
        if env_tmp_dir is not None:
            env_tmp_dir.cleanup()


def _validate_offline_prerequisites(path: str, auto_approve: bool = False) -> bool:
    """Validate requirements for offline deployment"""
    print("🔍 Validating offline deployment prerequisites...")
    
    # Check for pixi.toml
    pixi_toml = os.path.join(path, "pixi.toml")
    if not os.path.exists(pixi_toml):
        print("❌ pixi.toml not found")
        return False
    
    # Check for .pixi folder
    pixi_folder = os.path.join(path, ".pixi")
    if not os.path.exists(pixi_folder):
        print("❌ .pixi folder not found")
        print("💡 Run 'pixi install' first to create the environment")
        return False
    
    # Check for pixi.lock (indicates resolved dependencies)
    pixi_lock = os.path.join(path, "pixi.lock")
    if not os.path.exists(pixi_lock):
        print("❌ pixi.lock not found")
        print("💡 Run 'pixi install' first to resolve dependencies")
        return False
    
    # Check if .pixi folder has content
    pixi_contents = []
    try:
        for root, dirs, files in os.walk(pixi_folder):
            pixi_contents.extend(files)
    except OSError as e:
        print(f"❌ Error accessing .pixi folder: {e}")
        return False
    
    if len(pixi_contents) < 10:  # Arbitrary threshold
        print("⚠️ .pixi folder seems incomplete (very few files)")
        print("💡 Run 'pixi install' to ensure all dependencies are downloaded")
        
    # Get .pixi folder size
    pixi_size = 0
    try:
        for root, dirs, files in os.walk(pixi_folder):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    pixi_size += os.path.getsize(fp)
    except OSError as e:
        print(f"⚠️ Error calculating .pixi folder size: {e}")
    
    pixi_size_mb = pixi_size / (1024 * 1024)
    
    print(f"✅ Offline prerequisites validated")
    print(f"📁 .pixi folder size: {pixi_size_mb:.1f} MB")
    print(f"📄 Files in environment: {len(pixi_contents)}")
    
    # Warn about large packages with auto-approve support
    if pixi_size_mb > 500:
        print(f"⚠️ Large package size ({pixi_size_mb:.1f} MB) - upload may be slow")
        
        if auto_approve:
            print("✅ Auto-approve enabled - continuing with large package")
            return True
            
        try:
            confirm = input("Continue with large package? (y/N): ").strip().lower()
            if confirm not in ['y', 'yes']:
                print("❌ Offline deployment cancelled")
                return False
        except KeyboardInterrupt:
            print("\n❌ Offline deployment cancelled")
            return False
        
    return True


def _add_offline_metadata(tar_path: str, package_stats: dict, project_path: str):
    """Add metadata file to indicate offline package"""
    metadata = {
        "package_type": "offline",
        "pixi_included": True,
        "created_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        "stats": package_stats
    }
    
    # Add pixi.lock hash for future caching (Phase 2)
    pixi_lock_path = os.path.join(project_path, "pixi.lock")
    if os.path.exists(pixi_lock_path):
        try:
            with open(pixi_lock_path, 'rb') as f:
                metadata["pixi_lock_hash"] = hashlib.sha256(f.read()).hexdigest()
                print(f"🔑 pixi.lock hash: {metadata['pixi_lock_hash'][:16]}... (for future caching)")
        except Exception as e:
            print(f"⚠️ Could not calculate pixi.lock hash: {e}")
    
    # Write metadata to temp file and add to tar
    metadata_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    try:
        json.dump(metadata, metadata_file, indent=2)
        metadata_file.close()
        
        # Add to existing tar
        with tarfile.open(tar_path, "a") as tar:
            tar.add(metadata_file.name, ".offline_metadata.json")
        
    finally:
        os.unlink(metadata_file.name)


def _show_package_stats(package_stats: dict, offline_mode: bool, package_path: str):
    """Show package statistics"""
    package_size = os.path.getsize(package_path)
    package_size_mb = package_size / (1024 * 1024)
    
    print(f"\n📦 Package Statistics:")
    print(f"  Code files: {package_stats['code_files']}")
    
    if offline_mode:
        print(f"  Dependency files: {package_stats['dependency_files']}")
        pixi_mb = package_stats['pixi_size'] / (1024 * 1024)
        code_mb = (package_stats['total_size'] - package_stats['pixi_size']) / (1024 * 1024)
        print(f"  Code size: {code_mb:.1f} MB")
        print(f"  Dependencies size: {pixi_mb:.1f} MB")
        if package_stats['total_size'] > 0:
            compression_ratio = package_stats['total_size'] / package_size
            print(f"  Compression ratio: {compression_ratio:.1f}x")
    
    print(f"  Compressed package: {package_size_mb:.1f} MB")
    
    if offline_mode:
        print(f"  🔒 Mode: Offline (dependencies included)")
    else:
        print(f"  🌐 Mode: Online (dependencies will be downloaded)")


def _handle_obj(obj: dict):
    """Handle JSON object from server response (delegates to shared transport)"""
    transport._handle_obj(obj)


def _upload_and_run(pkg, task, headers, env_handler=None, on_obj=None):
    """Upload package and run task (delegates to shared transport)"""
    transport._upload_and_run(pkg, task, headers, on_obj=on_obj)


def _attach_stream_only(tid: str, headers):
    """Attach to running task - live stream only (delegates to shared transport)"""
    transport._attach_stream_only(tid, headers)


def _attach(tid: str, headers):
    """Attach to running task - stream only (delegates to shared transport)"""
    transport._attach(tid, headers)


def _attach_history(tid: str, headers):
    """Attach with full history (delegates to shared transport)"""
    transport._attach_history(tid, headers)


def perform_handshake(secret: str, rotate: bool):
    """Perform handshake to get AES key (delegates to shared transport)"""
    return transport.perform_handshake(secret, rotate)


def get_auto_approve_setting(cli_auto_approve: bool, config: Dict[str, Any]) -> bool:
    """Determine auto-approve setting with proper precedence: CLI > config > False"""
    
    # CLI argument has highest precedence
    if cli_auto_approve:
        print("✅ Auto-approve: enabled (CLI override)")
        return True
    
    # Check config file
    deployment_config = config.get("deployment", {})
    config_auto_approve = deployment_config.get("auto_approve", False)
    
    if config_auto_approve:
        print("✅ Auto-approve: enabled (from config)")
        return True
    
    # Default to interactive
    return False


# ───────────────────────── Ctrl-C menu (MATCHING ORIGINAL) ────────────────────────────────────
def _signal_handler(sig, frame):
    """Interactive signal handler with full menu (matching original)"""
    if transport.task_id is None:
        print("\nExiting...");
        sys.exit(0)

    print("\nInterrupted! Choose:")
    print("1) Terminate remote task and exit")
    print("2) Let task continue and exit")
    print("3) Restart remote task and exit")
    print("4) Redeploy current code to task")
    print("5) Continue monitoring")
    print("6) Restart task and keep monitoring")
    try:
        choice = input("Enter 1-6: ").strip()
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)

    hdr = transport.auth_headers
    if choice == "1":
        _http("DELETE", f"{transport.server_url}/task/{transport.task_id}", headers=hdr)
        print("Task terminated.")
        sys.exit(0)

    if choice == "2":
        print(f"Task {transport.task_id} left running.")
        sys.exit(0)

    if choice == "3":
        _http("POST", f"{transport.server_url}/task/{transport.task_id}/restart", headers=hdr)
        print("Task restarted.")
        sys.exit(0)

    if choice == "4":
        pkg = _package_dir()
        try:
            with open(pkg, "rb") as fh:
                files = {"file": (os.path.basename(pkg), fh, "application/octet-stream")}
                _http("POST", f"{transport.server_url}/task/{transport.task_id}/redeploy", headers=hdr, files=files)
        finally:
            os.unlink(pkg)
        print("Task redeployed.")
        sys.exit(0)

    if choice == "6":
        _http("POST", f"{transport.server_url}/task/{transport.task_id}/restart", headers=hdr)
        print("Task restarting...")
        _attach(transport.task_id, transport.auth_headers)
        return

    print("Continuing...")  # choice == "5"


# Register signal handler
signal.signal(signal.SIGINT, _signal_handler)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# FIXED ENVIRONMENT VARIABLE INJECTION (File-Based Approach)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

class EnvironmentHandler:
    """FIXED: Handles environment variable injection via file-based packaging"""
    
    def __init__(self, config: Dict[str, Any], task_id: str, client_info: Dict[str, Any]):
        self.config = config
        self.task_id = task_id
        self.client_info = client_info
        self.env_config = config.get("environment", {})
        
    def is_enabled(self) -> bool:
        """Check if environment injection is enabled"""
        return self.env_config.get("enabled", False)
        
    def get_environment_variables(self, profile: Optional[str] = None) -> Dict[str, str]:
        """Get all environment variables for injection"""
        if not self.is_enabled():
            return {}
            
        print(f"🌍 Processing environment variables for remote injection{f' (profile: {profile})' if profile else ''}...")
        
        env_vars = {}
        
        # 1. Base variables
        base_vars = self.env_config.get("variables", {})
        env_vars.update(base_vars)
        
        # 2. Profile-specific variables (override base)
        if profile:
            profile_vars = self.env_config.get(profile, {})
            env_vars.update(profile_vars)
            print(f"📋 Applied environment profile: {profile}")
        
        # 3. Conditional variables
        conditional_vars = self._resolve_conditional_variables()
        env_vars.update(conditional_vars)
        
        # 4. Resolve placeholders and templates
        resolved_vars = self._resolve_placeholders(env_vars)
        
        # 5. Handle secrets
        final_vars = self._resolve_secrets(resolved_vars)
        
        print(f"✅ Prepared {len(final_vars)} environment variables for injection")
        return final_vars
    
    def _resolve_conditional_variables(self) -> Dict[str, str]:
        """Resolve conditional environment variables based on config state"""
        conditional_config = self.env_config.get("conditional", {})
        result = {}
        
        for block_name, block_config in conditional_config.items():
            if isinstance(block_config, dict) and "condition" in block_config:
                condition = block_config["condition"]
                if self._evaluate_condition_path(condition):
                    # Add all variables from this block except 'condition'
                    for key, value in block_config.items():
                        if key != "condition":
                            result[key] = value
                    print(f"✅ Conditional block '{block_name}' applied")
                else:
                    print(f"⏭️ Conditional block '{block_name}' skipped")
            else:
                # Legacy format support (direct condition as key)
                if self._evaluate_condition(block_name):
                    result.update(block_config)
                    print(f"✅ Conditional block '{block_name}' applied")
                else:
                    print(f"⏭️ Conditional block '{block_name}' skipped")
        
        return result
    
    def _evaluate_condition_path(self, condition_path: str) -> bool:
        """Evaluate a condition path like 'mcp.filesystem.enabled'"""
        # Navigate through config to find the value
        current = self.config
        for part in condition_path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False

        # Convert to boolean
        if isinstance(current, bool):
            return current
        elif isinstance(current, str):
            return current.lower() in ["true", "yes", "1", "enabled"]
        else:
            return bool(current)

    def _evaluate_condition(self, condition: str) -> bool:
        """Evaluate a condition string like '${proxy.enabled}' or '${mcp.filesystem.enabled}'"""
        if not condition.startswith("${") or not condition.endswith("}"):
            return False

        return self._evaluate_condition_path(condition[2:-1])  # Remove ${ and }
    
    def _resolve_placeholders(self, env_vars: Dict[str, str]) -> Dict[str, str]:
        """Resolve placeholder variables like ${REMOTE_TASK_ID}"""
        resolved = {}
        
        # Available placeholders
        placeholders = {
            "REMOTE_TASK_ID": self.task_id,  # Note: This is packaging-time ID, not server-assigned ID
            "CLIENT_IP_ADDRESS": self.client_info.get("ip", "unknown"),
            "PROXY_PORT": str(self.client_info.get("proxy_port", 8080)),
            "DEPLOYMENT_TIME": str(int(time.time())),
            "CLIENT_HOSTNAME": self.client_info.get("hostname", "unknown")
        }
        
        # Add config-based placeholders
        if "proxy" in self.config:
            placeholders["proxy.port"] = str(self.config["proxy"].get("remote_port", 8080))
            placeholders["proxy.enabled"] = str(self.config["proxy"].get("enabled", False))
        
        if "mcp" in self.config:
            mcp_config = self.config["mcp"]
            if "filesystem" in mcp_config:
                placeholders["mcp.filesystem.root_path"] = mcp_config["filesystem"].get("root_path", ".")
                placeholders["mcp.filesystem.enabled"] = str(mcp_config["filesystem"].get("enabled", False))
        
        for key, value in env_vars.items():
            if isinstance(value, str):
                resolved_value = value
                # Replace placeholders
                for placeholder, replacement in placeholders.items():
                    resolved_value = resolved_value.replace(f"${{{placeholder}}}", replacement)
                resolved[key] = resolved_value
            else:
                resolved[key] = str(value)
        
        return resolved
    
    def _resolve_secrets(self, env_vars: Dict[str, str]) -> Dict[str, str]:
        """Resolve secret placeholders like ${vault:path} or ${env:VAR}"""
        resolved = {}
        
        for key, value in env_vars.items():
            if isinstance(value, str) and value.startswith("${") and ":" in value:
                try:
                    resolved_value = self._resolve_secret_placeholder(value)
                    resolved[key] = resolved_value
                except Exception as e:
                    print(f"⚠️ Failed to resolve secret for {key}: {e}")
                    resolved[key] = value  # Keep original if resolution fails
            else:
                resolved[key] = value
        
        return resolved
    
    def _resolve_secret_placeholder(self, placeholder: str) -> str:
        """Resolve a single secret placeholder"""
        if not placeholder.startswith("${") or not placeholder.endswith("}"):
            return placeholder
            
        inner = placeholder[2:-1]  # Remove ${ and }
        if ":" not in inner:
            return placeholder
            
        secret_type, path = inner.split(":", 1)
        
        if secret_type == "env":
            # Get from client environment
            return os.getenv(path, "")
        elif secret_type == "file":
            # Read from client file
            try:
                return Path(path).read_text().strip()
            except Exception:
                return ""
        elif secret_type == "vault":
            # Placeholder for vault integration
            print(f"⚠️ Vault secrets not implemented yet: {path}")
            return ""
        else:
            print(f"⚠️ Unknown secret type: {secret_type}")
            return placeholder
    
    def create_env_files_for_packaging(self, env_vars: Dict[str, str], profile: Optional[str] = None,
                                       target_dir: Optional[str] = None) -> Dict[str, str]:
        """FIXED: Create environment files in target_dir that get packaged with the code"""
        if not env_vars:
            return {}

        if target_dir is None:
            target_dir = tempfile.mkdtemp(prefix="env_files_")

        files_created = {}
        
        # 1. Create executable shell script for sourcing
        env_script_lines = [
            "#!/bin/bash",
            "# Environment variables injected by enhanced client",
            f"# Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Packaging Task ID: {self.task_id}",
            "# Note: PACKAGING_TASK_ID below is set at packaging time, actual server task ID may differ",
            "",
            "# Usage: source .env_injection.sh",
            ""
        ]
        
        for key, value in sorted(env_vars.items()):
            # Escape value for shell safety - FIXED escaping
            escaped_value = value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
            # Rename TASK_ID to be clearer about its origin
            if key == "TASK_ID":
                env_script_lines.append(f'export PACKAGING_TASK_ID="{escaped_value}"')
                env_script_lines.append('# Note: PACKAGING_TASK_ID is the temporary ID used during packaging')
                env_script_lines.append('# The actual server-assigned task ID can be found in container environment or logs')
            else:
                env_script_lines.append(f'export {key}="{escaped_value}"')
        
        env_script_content = "\n".join(env_script_lines)

        # Write to temp dir for packaging (arcname: .env_injection.sh)
        env_script_path = os.path.join(target_dir, ".env_injection.sh")
        with open(env_script_path, "w") as f:
            f.write(env_script_content)
        os.chmod(env_script_path, 0o755)  # Make executable

        files_created[".env_injection.sh"] = env_script_content
        
        # 2. Create .env file for applications that expect it
        env_file_lines = [
            f"# Environment variables for task: {self.task_id}",
            f"# Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]
        
        for key, value in sorted(env_vars.items()):
            # Rename TASK_ID to be clearer
            display_key = "PACKAGING_TASK_ID" if key == "TASK_ID" else key
            
            # Use standard .env format (no quotes unless necessary)
            if " " in value or '"' in value or "\n" in value:
                escaped_value = value.replace('"', '\\"')
                env_file_lines.append(f'{display_key}="{escaped_value}"')
            else:
                env_file_lines.append(f"{display_key}={value}")
                
        # Add note about task ID
        if "TASK_ID" in env_vars:
            env_file_lines.append("")
            env_file_lines.append("# Note: PACKAGING_TASK_ID is the temporary ID used during packaging")
            env_file_lines.append("# The actual server-assigned task ID differs and can be found in container environment")
        
        env_file_content = "\n".join(env_file_lines)
        env_file_path = os.path.join(target_dir, ".env")

        with open(env_file_path, "w") as f:
            f.write(env_file_content)

        files_created[".env"] = env_file_content
        
        # 3. Create JSON file for programmatic access
        env_json = {
            "metadata": {
                "packaging_task_id": self.task_id,
                "generated_at": time.strftime('%Y-%m-%d %H:%M:%S'),
                "client_info": self.client_info,
                "note": "packaging_task_id is the temporary ID used during packaging, actual server task ID differs",
                "usage": {
                    "shell": "source .env_injection.sh",
                    "dotenv": "Load .env file in your application",
                    "json": "Parse .env_vars.json programmatically"
                }
            },
            "variables": {
                # Rename TASK_ID key to be clearer
                **{("PACKAGING_TASK_ID" if k == "TASK_ID" else k): v for k, v in env_vars.items()}
            }
        }
        
        env_json_path = os.path.join(target_dir, ".env_vars.json")
        with open(env_json_path, "w") as f:
            json.dump(env_json, f, indent=2)

        files_created[".env_vars.json"] = json.dumps(env_json, indent=2)
        
        print(f"✅ Created {len(files_created)} environment files for packaging:")
        for file_path in files_created.keys():
            print(f"    📄 {file_path}")
        
        return files_created


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# NEW: EXTERNAL MCP SERVER ROUTING SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════════════════════

class MCPAuthHandler:
    """Base class for MCP server authentication"""
    
    def get_headers(self) -> Dict[str, str]:
        """Get authentication headers"""
        return {}
    
    def get_auth_params(self) -> Dict[str, Any]:
        """Get authentication parameters for requests"""
        return {}


class BearerTokenAuth(MCPAuthHandler):
    """Bearer token authentication for MCP servers"""
    
    def __init__(self, token: str):
        self.token = token
    
    def get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class ApiKeyAuth(MCPAuthHandler):
    """API key authentication for MCP servers"""
    
    def __init__(self, api_key: str, header_name: str = "X-API-Key"):
        self.api_key = api_key
        self.header_name = header_name
    
    def get_headers(self) -> Dict[str, str]:
        return {self.header_name: self.api_key}


class NoAuth(MCPAuthHandler):
    """No authentication for MCP servers"""
    pass


class MCPServerTransformer:
    """Base class for transforming requests/responses for different MCP server types"""
    
    def transform_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Transform internal request format to external server format"""
        return request
    
    def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform external server response to internal format"""
        return response


class MindsDBTransformer(MCPServerTransformer):
    """Transformer for MindsDB MCP server"""
    
    def transform_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Transform request for MindsDB format"""
        action = request.get("action", "")
        params = request.get("params", {})
        
        # Map our internal actions to MindsDB MCP actions
        action_mapping = {
            "mindsdb_query": "sql_query",
            "mindsdb_predict": "predict",
            "mindsdb_list_models": "list_models",
            "mindsdb_create_model": "create_model"
        }
        
        external_action = action_mapping.get(action, action)
        
        return {
            "method": "tools/call",
            "params": {
                "name": external_action,
                "arguments": params
            },
            "id": request.get("request_id", str(uuid.uuid4()))
        }
    
    def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform MindsDB response to our format"""
        if "result" in response:
            return {
                "success": True,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(response["result"], indent=2)
                    }]
                }
            }
        elif "error" in response:
            return {
                "success": False,
                "error": response["error"]
            }
        else:
            return response


class GenericMCPTransformer(MCPServerTransformer):
    """Generic transformer for standard MCP servers"""
    
    def transform_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Transform to standard MCP format"""
        return {
            "jsonrpc": "2.0",
            "id": request.get("request_id", str(uuid.uuid4())),
            "method": "tools/call",
            "params": {
                "name": request.get("action", ""),
                "arguments": request.get("params", {})
            }
        }
    
    def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform standard MCP response"""
        if "result" in response:
            return {
                "success": True,
                "result": response["result"]
            }
        elif "error" in response:
            return {
                "success": False,
                "error": response["error"]["message"] if isinstance(response["error"], dict) else str(response["error"])
            }
        else:
            return response


class MCPExternalProxy:
    """Routes MCP requests to external MCP servers"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.servers = self._load_external_servers()
        self.routing_table = self._build_routing_table()
        self.transformers = self._create_transformers()
        self.auth_handlers = self._create_auth_handlers()
        self._server_health = {}  # Track server health status
        
    def _load_external_servers(self) -> List[Dict[str, Any]]:
        """Load external server configurations"""
        external_config = self.config.get("mcp", {}).get("external_servers", {})
        if not external_config.get("enabled", False):
            return []
        
        servers = external_config.get("server", [])
        if isinstance(servers, dict):
            servers = [servers]  # Handle single server config
        elif not isinstance(servers, list):
            servers = []
        
        print(f"🔗 Loading {len(servers)} external MCP servers")
        
        if len(servers) == 0:
            print("⚠️ No external MCP servers configured in config.mcp.external_servers.server")
            print("💡 Add servers with: [[config.mcp.external_servers.server]]")
            return []
        
        for server in servers:
            name = server.get("name", "unnamed")
            url = server.get("url", "")
            actions = server.get("actions", [])
            print(f"    📍 {name}: {url} (actions: {len(actions)})")
        
        return servers
    
    def _build_routing_table(self) -> Dict[str, List[Dict[str, Any]]]:
        """Build action -> servers routing table"""
        routing_table = {}
        
        for server in self.servers:
            actions = server.get("actions", [])
            priority = server.get("priority", 10)
            
            for action in actions:
                if action not in routing_table:
                    routing_table[action] = []
                
                # Insert by priority (lower number = higher priority)
                server_entry = {**server, "priority": priority}
                inserted = False
                for i, existing in enumerate(routing_table[action]):
                    if priority < existing["priority"]:
                        routing_table[action].insert(i, server_entry)
                        inserted = True
                        break
                
                if not inserted:
                    routing_table[action].append(server_entry)
        
        return routing_table
    
    def _create_transformers(self) -> Dict[str, MCPServerTransformer]:
        """Create transformers for different server types"""
        return {
            "mindsdb": MindsDBTransformer(),
            "generic": GenericMCPTransformer()
        }
    
    def _create_auth_handlers(self) -> Dict[str, MCPAuthHandler]:
        """Create authentication handlers for servers"""
        handlers = {}
        
        for server in self.servers:
            name = server.get("name")
            auth_type = server.get("auth_type", "none")
            
            if auth_type == "bearer":
                token = server.get("auth_token", "")
                if token.startswith("${env:"):
                    # Resolve environment variable
                    env_var = token[6:-1]  # Remove ${env: and }
                    token = os.getenv(env_var, "")
                handlers[name] = BearerTokenAuth(token)
            elif auth_type == "api_key":
                api_key = server.get("auth_token", "")
                header_name = server.get("auth_header", "X-API-Key")
                if api_key.startswith("${env:"):
                    env_var = api_key[6:-1]
                    api_key = os.getenv(env_var, "")
                handlers[name] = ApiKeyAuth(api_key, header_name)
            else:
                handlers[name] = NoAuth()
        
        return handlers
    
    def can_handle(self, action: str) -> bool:
        """Check if any external server can handle this action"""
        # Check exact match
        if action in self.routing_table:
            return True
        
        # Check wildcard patterns
        for pattern in self.routing_table.keys():
            if self._action_matches_pattern(action, pattern):
                return True
        
        return False
    
    def _action_matches_pattern(self, action: str, pattern: str) -> bool:
        """Check if action matches a wildcard pattern"""
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return action.startswith(prefix)
        return action == pattern
    
    def _find_servers_for_action(self, action: str) -> List[Dict[str, Any]]:
        """Find servers that can handle this action"""
        servers = []
        
        # Check exact match first
        if action in self.routing_table:
            servers.extend(self.routing_table[action])
        
        # Check wildcard patterns
        for pattern, pattern_servers in self.routing_table.items():
            if self._action_matches_pattern(action, pattern):
                servers.extend(pattern_servers)
        
        # Remove duplicates and sort by priority
        seen = set()
        unique_servers = []
        for server in servers:
            server_id = server.get("name")
            if server_id not in seen:
                seen.add(server_id)
                unique_servers.append(server)
        
        return sorted(unique_servers, key=lambda s: s.get("priority", 10))
    
    async def route_request(self, mcp_request: Dict[str, Any]) -> Dict[str, Any]:
        """Route request to appropriate external server"""
        action = mcp_request.get("action", "")
        servers = self._find_servers_for_action(action)
        
        if not servers:
            return {
                "success": False,
                "error": f"No external server configured for action: {action}"
            }
        
        # Try servers in priority order
        last_error = None
        for server in servers:
            try:
                print(f"🔄 Routing {action} to {server['name']}")
                result = await self._proxy_to_external(server, mcp_request)
                if result.get("success", True):  # Consider success if no explicit failure
                    return result
                else:
                    last_error = result.get("error", "Unknown error")
                    print(f"⚠️ Server {server['name']} returned error: {last_error}")
            except Exception as e:
                last_error = str(e)
                print(f"❌ Failed to reach server {server['name']}: {e}")
                self._mark_server_unhealthy(server["name"])
                continue
        
        return {
            "success": False,
            "error": f"All servers failed for action {action}. Last error: {last_error}"
        }
    
    async def _proxy_to_external(self, server_config: Dict, request: Dict) -> Dict:
        """Forward request to external MCP server"""
        if not AIOHTTP_AVAILABLE:
            return {
                "success": False,
                "error": "aiohttp not available - cannot reach external MCP servers"
            }
            
        server_name = server_config.get("name")
        server_url = server_config.get("url")
        server_type = server_config.get("type", "generic")
        timeout = server_config.get("timeout", 30)
        
        # Get transformer and auth handler
        transformer = self.transformers.get(server_type, self.transformers["generic"])
        auth_handler = self.auth_handlers.get(server_name, NoAuth())
        
        # Transform request to external format
        external_request = transformer.transform_request(request)
        
        # Build headers
        headers = {
            "Content-Type": "application/json",
            **auth_handler.get_headers()
        }
        
        print(f"📡 Sending request to {server_name} at {server_url}")
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                # Determine endpoint based on server type
                endpoint = server_config.get("endpoint", "/mcp/request")
                if server_type == "mindsdb":
                    endpoint = "/api/mcp"  # MindsDB specific endpoint
                
                full_url = f"{server_url.rstrip('/')}{endpoint}"
                
                async with session.post(
                    full_url,
                    json=external_request,
                    headers=headers
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        self._mark_server_healthy(server_name)
                        
                        # Transform response back to our format
                        transformed_result = transformer.transform_response(result)
                        print(f"✅ Received response from {server_name}")
                        return transformed_result
                    else:
                        error_text = await response.text()
                        print(f"❌ Server {server_name} returned {response.status}: {error_text}")
                        return {
                            "success": False,
                            "error": f"Server error {response.status}: {error_text}"
                        }
                        
        except asyncio.TimeoutError:
            print(f"⏰ Timeout connecting to {server_name}")
            self._mark_server_unhealthy(server_name)
            return {
                "success": False,
                "error": f"Timeout connecting to {server_name}"
            }
        except Exception as e:
            print(f"❌ Error connecting to {server_name}: {e}")
            self._mark_server_unhealthy(server_name)
            return {
                "success": False,
                "error": f"Connection error: {str(e)}"
            }
    
    def _mark_server_healthy(self, server_name: str):
        """Mark server as healthy"""
        self._server_health[server_name] = {
            "status": "healthy",
            "last_success": time.time()
        }
    
    def _mark_server_unhealthy(self, server_name: str):
        """Mark server as unhealthy"""
        self._server_health[server_name] = {
            "status": "unhealthy",
            "last_failure": time.time()
        }
    
    async def health_check_servers(self):
        """Perform health checks on all configured servers"""
        if not AIOHTTP_AVAILABLE:
            print("❌ Cannot perform health checks - aiohttp not available")
            return
            
        print("🏥 Performing health checks on external MCP servers...")
        
        for server in self.servers:
            name = server.get("name")
            url = server.get("url")
            
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    health_endpoint = f"{url.rstrip('/')}/health"
                    async with session.get(health_endpoint) as response:
                        if response.status == 200:
                            self._mark_server_healthy(name)
                            print(f"    ✅ {name}: healthy")
                        else:
                            self._mark_server_unhealthy(name)
                            print(f"    ❌ {name}: unhealthy ({response.status})")
            except Exception as e:
                self._mark_server_unhealthy(name)
                print(f"    ❌ {name}: unreachable ({e})")
    
    def get_server_status(self) -> Dict[str, Any]:
        """Get status of all external servers"""
        return {
            "enabled": len(self.servers) > 0,
            "server_count": len(self.servers),
            "servers": [
                {
                    "name": server.get("name"),
                    "url": server.get("url"),
                    "type": server.get("type", "generic"),
                    "actions": server.get("actions", []),
                    "health": self._server_health.get(server.get("name"), {"status": "unknown"})
                }
                for server in self.servers
            ],
            "routing_actions": list(self.routing_table.keys())
        }


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# NEW: HTTP(S) REVERSE PROXY HANDLER
# ═══════════════════════════════════════════════════════════════════════════════════════════════

class ProxyRequestHandler:
    """Handles HTTP(S) reverse proxy requests from remote tasks"""
    
    def __init__(self, config: Dict[str, Any], client_info: Dict[str, Any]):
        self.config = config
        self.client_info = client_info
        self.proxy_config = config.get("proxy", {})
        self.mappings = self._load_proxy_mappings()
        
    def is_enabled(self) -> bool:
        """Check if proxy is enabled"""
        return self.proxy_config.get("enabled", False)
    
    def _load_proxy_mappings(self) -> List[Dict[str, Any]]:
        """Load proxy URL mappings from config"""
        mappings = []
        
        # Handle both single mapping and multiple mappings
        if "mapping" in self.proxy_config:
            mapping_config = self.proxy_config["mapping"]
            if isinstance(mapping_config, list):
                mappings.extend(mapping_config)
            elif isinstance(mapping_config, dict):
                mappings.append(mapping_config)
        
        # Also check for legacy format
        for key, value in self.proxy_config.items():
            if key.startswith("mapping.") and isinstance(value, dict):
                value["name"] = key.replace("mapping.", "")
                mappings.append(value)
        
        print(f"🔗 Loaded {len(mappings)} proxy mappings")
        for mapping in mappings:
            name = mapping.get("name", "unnamed")
            remote_path = mapping.get("remote_path", "/*")
            local_url = mapping.get("local_url", "http://localhost")
            print(f"    📍 {name}: {remote_path} → {local_url}")
        
        return mappings
    
    def get_mapping_info(self) -> Dict[str, Any]:
        """Get detailed information about proxy mappings for display"""
        proxy_port = self.proxy_config.get("remote_port", 8080)
        
        mapping_info = {
            "enabled": self.is_enabled(),
            "remote_port": proxy_port,
            "mappings": []
        }
        
        for i, mapping in enumerate(self.mappings, 1):
            name = mapping.get("name", f"mapping-{i}")
            remote_path = mapping.get("remote_path", "/*")
            local_url = mapping.get("local_url", "http://localhost")
            
            # Generate test URLs
            test_urls = []
            if remote_path.endswith("/*"):
                base_path = remote_path[:-2]
                test_urls = [
                    f"http://localhost:{proxy_port}{base_path}/health",
                    f"http://localhost:{proxy_port}{base_path}/status"
                ]
                if base_path == "/api":
                    test_urls.append(f"http://localhost:{proxy_port}{base_path}/users")
            elif remote_path == "/*":
                test_urls = [
                    f"http://localhost:{proxy_port}/",
                    f"http://localhost:{proxy_port}/health"
                ]
            else:
                test_urls = [f"http://localhost:{proxy_port}{remote_path}"]
            
            mapping_info["mappings"].append({
                "name": name,
                "remote_path": remote_path,
                "remote_url": f"http://localhost:{proxy_port}{remote_path}",
                "local_url": local_url,
                "test_urls": test_urls,
                "transformations": {
                    "add_headers": len(mapping.get("add_headers", {})),
                    "remove_headers": len(mapping.get("remove_headers", [])),
                    "replace_headers": len(mapping.get("replace_headers", {}))
                }
            })
        
        return mapping_info
    
    def find_mapping(self, request_path: str) -> Optional[Dict[str, Any]]:
        """Find the best matching proxy mapping for a request path"""
        best_match = None
        best_score = -1
        
        for mapping in self.mappings:
            remote_path = mapping.get("remote_path", "/*")
            
            # Convert glob pattern to regex
            pattern = self._glob_to_regex(remote_path)
            
            if re.match(pattern, request_path):
                # Score by specificity (longer patterns are more specific)
                score = len(remote_path.replace("*", ""))
                if score > best_score:
                    best_score = score
                    best_match = mapping
        
        return best_match
    
    def _glob_to_regex(self, pattern: str) -> str:
        """Convert glob pattern to regex pattern"""
        # Escape special regex chars except *
        escaped = re.escape(pattern).replace(r'\*', '.*')
        return f"^{escaped}$"
    
    def process_request(self, proxy_request: Dict[str, Any]) -> Dict[str, Any]:
        """Process an HTTP proxy request and return response"""
        try:
            method = proxy_request.get("method", "GET")
            path = proxy_request.get("path", "/")
            headers = proxy_request.get("headers", {})
            body = proxy_request.get("body", "")
            query_params = proxy_request.get("query_params", {})
            
            print(f"🌐 Processing {method} {path}")
            
            # Find matching mapping
            mapping = self.find_mapping(path)
            if not mapping:
                return {
                    "status": 404,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": "No proxy mapping found for path", "path": path})
                }
            
            mapping_name = mapping.get("name", "unnamed")
            local_url = mapping.get("local_url", "http://localhost")
            
            print(f"🎯 Using mapping '{mapping_name}': {local_url}")
            
            # Transform headers
            final_headers = self._transform_headers(headers, mapping)
            
            # Build target URL
            target_url = self._build_target_url(local_url, path, mapping, query_params)
            
            print(f"📡 Forwarding to: {target_url}")
            
            # Make the request
            response = self._make_local_request(method, target_url, final_headers, body, mapping)
            
            return response
            
        except Exception as e:
            print(f"❌ Proxy request error: {e}")
            import traceback
            traceback.print_exc()
            
            return {
                "status": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Proxy processing error: {str(e)}"})
            }
    
    def _transform_headers(self, original_headers: Dict[str, str], mapping: Dict[str, Any]) -> Dict[str, str]:
        """Transform headers according to mapping configuration"""
        headers = dict(original_headers)  # Copy original headers
        
        # Remove headers
        remove_headers = mapping.get("remove_headers", [])
        for header_name in remove_headers:
            headers.pop(header_name, None)
            print(f"🗑️ Removed header: {header_name}")
        
        # Add headers
        add_headers = mapping.get("add_headers", {})
        for header_name, header_value in add_headers.items():
            resolved_value = self._resolve_header_value(header_value)
            headers[header_name] = resolved_value
            print(f"➕ Added header: {header_name}=***")

        # Replace headers
        replace_headers = mapping.get("replace_headers", {})
        for header_name, header_value in replace_headers.items():
            resolved_value = self._resolve_header_value(header_value)
            headers[header_name] = resolved_value
            print(f"🔄 Replaced header: {header_name}=***")
        
        return headers
    
    def _resolve_header_value(self, value: str) -> str:
        """Resolve placeholders in header values"""
        if not isinstance(value, str):
            return str(value)
        
        # Available placeholders for header resolution
        placeholders = {
            "CLIENT_IP_ADDRESS": self.client_info.get("ip", "unknown"),
            "CLIENT_HOSTNAME": self.client_info.get("hostname", "unknown"),
            "PROXY_PORT": str(self.client_info.get("proxy_port", 8080)),
            "TIMESTAMP": str(int(time.time())),
            "DEPLOYMENT_TIME": str(int(time.time()))
        }
        
        resolved_value = value
        for placeholder, replacement in placeholders.items():
            resolved_value = resolved_value.replace(f"${{{placeholder}}}", replacement)
        
        # Handle environment variables
        def replace_env(match):
            env_var = match.group(1)
            return os.getenv(env_var, "")
        
        resolved_value = re.sub(r'\$\{env:([^}]+)\}', replace_env, resolved_value)
        
        # Handle file references
        def replace_file(match):
            file_path = match.group(1)
            try:
                return Path(file_path).read_text().strip()
            except Exception:
                return ""
        
        resolved_value = re.sub(r'\$\{file:([^}]+)\}', replace_file, resolved_value)
        
        return resolved_value
    
    def _build_target_url(self, base_url: str, request_path: str, mapping: Dict[str, Any], query_params: Dict[str, str]) -> str:
        """Build the target URL for the local service"""
        # Parse base URL
        parsed_base = urllib.parse.urlparse(base_url)
        
        # Handle path transformation
        remote_path = mapping.get("remote_path", "/*")
        local_path_prefix = mapping.get("local_path_prefix", "")
        
        # Transform the path
        if remote_path.endswith("/*"):
            # Strip the prefix and add local prefix
            prefix_to_remove = remote_path[:-2]  # Remove /*
            if request_path.startswith(prefix_to_remove):
                remaining_path = request_path[len(prefix_to_remove):]
                final_path = local_path_prefix + remaining_path
            else:
                final_path = local_path_prefix + request_path
        else:
            # Exact match
            final_path = local_path_prefix or parsed_base.path
        
        # Ensure path starts with /
        if not final_path.startswith("/"):
            final_path = "/" + final_path
        
        # Build query string
        query_string = ""
        if query_params:
            query_string = urllib.parse.urlencode(query_params)
        
        # Combine everything
        target_url = urllib.parse.urlunparse((
            parsed_base.scheme,
            parsed_base.netloc,
            final_path,
            "",  # params
            query_string,
            ""   # fragment
        ))
        
        return target_url
    
    def _make_local_request(self, method: str, url: str, headers: Dict[str, str], body: str, mapping: Dict[str, Any]) -> Dict[str, Any]:
        """Make the actual request to the local service"""
        try:
            timeout = mapping.get("timeout", 30)
            
            # Prepare request kwargs
            kwargs = {
                "timeout": timeout,
                "verify": mapping.get("verify_ssl", True),
                "allow_redirects": mapping.get("follow_redirects", True)
            }
            
            # Add body for methods that support it
            if method.upper() in ["POST", "PUT", "PATCH"] and body:
                if isinstance(body, str):
                    kwargs["data"] = body
                else:
                    kwargs["json"] = body
            
            # Add headers
            if headers:
                kwargs["headers"] = headers
            
            # Make the request
            response = _http_session.request(method, url, **kwargs)
            
            # Convert response
            response_headers = dict(response.headers)
            
            # Handle binary content
            try:
                response_body = response.text
            except UnicodeDecodeError:
                response_body = base64.b64encode(response.content).decode('ascii')
                response_headers["X-Proxy-Content-Encoding"] = "base64"
            
            print(f"✅ Local request successful: {response.status_code}")
            
            return {
                "status": response.status_code,
                "headers": response_headers,
                "body": response_body
            }
            
        except requests.exceptions.Timeout:
            print(f"⏰ Request timeout to {url}")
            return {
                "status": 504,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Gateway timeout", "url": url})
            }
        
        except requests.exceptions.ConnectionError:
            print(f"🔌 Connection error to {url}")
            return {
                "status": 502,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Bad gateway - service unavailable", "url": url})
            }
        
        except Exception as e:
            print(f"❌ Request error to {url}: {e}")
            return {
                "status": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Internal proxy error: {str(e)}", "url": url})
            }


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# NEW: LOCAL HTTP PROXY SERVER FOR TESTING
# ═══════════════════════════════════════════════════════════════════════════════════════════════

class LocalProxyTestServer:
    """Local HTTP server that simulates remote task making proxy requests"""
    
    def __init__(self, proxy_handler: ProxyRequestHandler, port: int = 8081):
        self.proxy_handler = proxy_handler
        self.port = port
        self.server = None
        self.server_thread = None
        
    def start(self):
        """Start the local proxy test server"""
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            from urllib.parse import urlparse, parse_qs
            import json
            
            proxy_handler = self.proxy_handler
            
            class ProxyTestRequestHandler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass  # Suppress default logging
                
                def do_GET(self):
                    self.handle_request("GET")
                
                def do_POST(self):
                    self.handle_request("POST")
                
                def do_PUT(self):
                    self.handle_request("PUT")
                
                def do_DELETE(self):
                    self.handle_request("DELETE")
                
                def handle_request(self, method):
                    """Handle HTTP request by proxying to local services"""
                    try:
                        parsed_path = urlparse(self.path)
                        query_params = parse_qs(parsed_path.query)
                        
                        # Get request body
                        content_length = int(self.headers.get('Content-Length', 0))
                        body = ""
                        if content_length > 0:
                            body = self.rfile.read(content_length).decode('utf-8')
                        
                        print(f"🌐 Local Test Proxy: {method} {self.path}")
                        
                        # Create proxy request
                        proxy_request = {
                            "method": method,
                            "path": parsed_path.path,
                            "headers": dict(self.headers),
                            "body": body,
                            "query_params": {k: v[0] if v else '' for k, v in query_params.items()}
                        }
                        
                        # Process through proxy handler
                        response = proxy_handler.process_request(proxy_request)
                        
                        # Send response
                        self.send_response(response.get("status", 200))
                        
                        # Send headers
                        response_headers = response.get("headers", {})
                        for key, value in response_headers.items():
                            self.send_header(key, value)
                        self.end_headers()
                        
                        # Send body
                        response_body = response.get("body", "")
                        self.wfile.write(response_body.encode('utf-8'))
                        
                        print(f"✅ Local Test Proxy: {response.get('status', 200)} ({len(response_body)} bytes)")
                        
                    except Exception as e:
                        print(f"❌ Local Test Proxy Error: {e}")
                        self.send_response(500)
                        self.send_header('Content-Type', 'application/json')
                        self.end_headers()
                        error_response = json.dumps({"error": f"Proxy test error: {str(e)}"})
                        self.wfile.write(error_response.encode('utf-8'))
            
            self.server = HTTPServer(('localhost', self.port), ProxyTestRequestHandler)
            
            def run_server():
                print(f"🧪 Local proxy test server starting on http://localhost:{self.port}")
                print(f"💡 You can now test with: curl http://localhost:{self.port}/api/health")
                self.server.serve_forever()
            
            self.server_thread = threading.Thread(target=run_server, daemon=True)
            self.server_thread.start()
            
            import time
            time.sleep(0.5)  # Give server time to start
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to start local proxy test server: {e}")
            return False
    
    def stop(self):
        """Stop the local proxy test server"""
        if self.server:
            print(f"🛑 Stopping local proxy test server on port {self.port}")
            self.server.shutdown()
            self.server = None
        if self.server_thread:
            self.server_thread.join(timeout=1)
            self.server_thread = None


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# MCP BACK-CHANNEL CLASSES (Enhanced with Proxy Support)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

def _resolve_within_root(root_path: str, file_path: str) -> Optional[Path]:
    """Resolve file_path inside root_path, rejecting traversal/symlink escapes."""
    real = Path(root_path).resolve()
    target = (real / file_path).resolve()
    if not target.is_relative_to(real):
        return None
    return target


class MCPBackChannelHandler:
    """Enhanced MCP handler with external server routing + built-in filesystem support"""
    
    def __init__(self):
        self.mcp_servers = {}
        self.request_handlers = {}
        self.external_proxy = None  # Will be set if external servers are configured
        
    def configure_external_servers(self, config: Dict[str, Any]):
        """Configure external MCP server routing"""
        external_config = config.get("mcp", {}).get("external_servers", {})
        if not external_config.get("enabled", False):
            print(f"🔒 External MCP servers disabled - using built-in handlers only")
            return
            
        if not AIOHTTP_AVAILABLE:
            print(f"❌ External MCP servers require aiohttp - install with: pip install aiohttp")
            return
            
        self.external_proxy = MCPExternalProxy(config)
        print(f"🌐 External MCP server routing enabled")
        
        # Note: Health check will be performed later when we have an async context
        
    def register_filesystem_server(self, name: str = "filesystem", root_path: str = "."):
        """Register built-in MCP filesystem server"""
        abs_root = os.path.abspath(root_path)
        print(f"🗂️  Registering built-in MCP Filesystem Server: {name}")
        print(f"📁 Root path: {abs_root}")
        
        self.mcp_servers[name] = {
            "type": "filesystem",
            "root_path": abs_root,
            "tools": ["read_file", "write_file", "create_directory", "list_directory"]
        }
        
        # Register request handlers
        self.request_handlers[f"{name}_read_file"] = self._handle_read_file
        self.request_handlers[f"{name}_write_file"] = self._handle_write_file
        self.request_handlers[f"{name}_list_directory"] = self._handle_list_directory
        self.request_handlers[f"{name}_create_directory"] = self._handle_create_directory
        
        print(f"✅ Built-in MCP Filesystem Server '{name}' registered")
    
    async def route_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Route MCP request to external servers or built-in handlers"""
        action = request.get("action", "")
        
        # 1. Try external servers first (if configured and can handle the action)
        if self.external_proxy and self.external_proxy.can_handle(action):
            print(f"🌐 Routing {action} to external MCP server")
            try:
                result = await self.external_proxy.route_request(request)
                if result.get("success", True):
                    return result
                else:
                    # If external server fails, try built-in fallback
                    external_config = self.external_proxy.config.get("mcp", {}).get("external_servers", {})
                    if external_config.get("fallback_to_builtin", True):
                        print(f"⤵️ External server failed, falling back to built-in handler")
                    else:
                        return result  # Return external error if fallback disabled
            except Exception as e:
                print(f"❌ External server routing failed: {e}")
                # Continue to built-in fallback
        
        # 2. Try built-in handlers
        if action.startswith("filesystem_"):
            print(f"🗂️ Handling {action} with built-in filesystem server")
            return await self._handle_builtin_request(request)
        
        # 3. No handler found
        return {
            "success": False,
            "error": f"No MCP handler available for action: {action}",
            "request_id": request.get("request_id")
        }
    
    async def _handle_builtin_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle request with built-in filesystem handlers"""
        action = request.get("action", "")
        params = request.get("params", {})
        request_id = request.get("request_id", str(uuid.uuid4()))
        
        try:
            if action == "filesystem_write_file":
                path = params.get("path", "")
                content = params.get("content", "")
                
                print(f"📝 Writing file via built-in MCP: {path} ({len(content)} bytes)")
                
                result = self._handle_write_file({
                    "arguments": {"path": path, "content": content},
                    "server": "filesystem"
                })
                
                return {
                    "success": True,
                    "request_id": request_id,
                    "action": action,
                    "result": result
                }
                
            elif action == "filesystem_read_file":
                path = params.get("path", "")
                
                print(f"📖 Reading file via built-in MCP: {path}")
                
                result = self._handle_read_file({
                    "arguments": {"path": path},
                    "server": "filesystem"
                })
                
                return {
                    "success": True,
                    "request_id": request_id,
                    "action": action,
                    "result": result
                }
                
            elif action == "filesystem_list_directory":
                path = params.get("path", ".")
                
                print(f"📂 Listing directory via built-in MCP: {path}")
                
                result = self._handle_list_directory({
                    "arguments": {"path": path},
                    "server": "filesystem"
                })
                
                return {
                    "success": True,
                    "request_id": request_id,
                    "action": action,
                    "result": result
                }
                
            elif action == "filesystem_create_directory":
                path = params.get("path", "")
                
                print(f"📁 Creating directory via built-in MCP: {path}")
                
                result = self._handle_create_directory({
                    "arguments": {"path": path},
                    "server": "filesystem"
                })
                
                return {
                    "success": True,
                    "request_id": request_id,
                    "action": action,
                    "result": result
                }
                
            else:
                return {
                    "success": False,
                    "request_id": request_id,
                    "error": f"Unknown built-in MCP action: {action}"
                }
                
        except Exception as e:
            print(f"❌ Built-in MCP handler error for {action}: {e}")
            return {
                "success": False,
                "request_id": request_id,
                "error": f"Built-in MCP handler error: {str(e)}"
            }
    
    def _handle_read_file(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle MCP read_file request"""
        try:
            file_path = request.get("arguments", {}).get("path", "")
            server_name = request.get("server", "filesystem")
            root_path = self.mcp_servers[server_name]["root_path"]

            # Security: ensure path is within root
            abs_path = _resolve_within_root(root_path, file_path)
            if abs_path is None:
                return {"error": "Path outside of allowed root directory"}

            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return {
                "content": [{
                    "type": "text",
                    "text": content
                }]
            }
        except Exception as e:
            return {"error": f"Failed to read file: {str(e)}"}
    
    def _handle_write_file(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle MCP write_file request"""
        try:
            args = request.get("arguments", {})
            file_path = args.get("path", "")
            content = args.get("content", "")
            server_name = request.get("server", "filesystem")
            root_path = self.mcp_servers[server_name]["root_path"]

            # Security: ensure path is within root
            abs_path = _resolve_within_root(root_path, file_path)
            if abs_path is None:
                return {"error": "Path outside of allowed root directory"}

            # Create directory if it doesn't exist
            os.makedirs(abs_path.parent, exist_ok=True)

            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            return {
                "content": [{
                    "type": "text",
                    "text": f"Successfully wrote to {file_path}"
                }]
            }
        except Exception as e:
            return {"error": f"Failed to write file: {str(e)}"}
    
    def _handle_list_directory(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle MCP list_directory request"""
        try:
            dir_path = request.get("arguments", {}).get("path", ".")
            server_name = request.get("server", "filesystem")
            root_path = self.mcp_servers[server_name]["root_path"]

            # Security: ensure path is within root
            abs_path = _resolve_within_root(root_path, dir_path)
            if abs_path is None:
                return {"error": "Path outside of allowed root directory"}

            items = []
            for item in os.listdir(abs_path):
                item_path = os.path.join(abs_path, item)
                items.append({
                    "name": item,
                    "type": "directory" if os.path.isdir(item_path) else "file",
                    "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None
                })
            
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(items, indent=2)
                }]
            }
        except Exception as e:
            return {"error": f"Failed to list directory: {str(e)}"}
    
    def _handle_create_directory(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle MCP create_directory request"""
        try:
            dir_path = request.get("arguments", {}).get("path", "")
            server_name = request.get("server", "filesystem")
            root_path = self.mcp_servers[server_name]["root_path"]

            # Security: ensure path is within root
            abs_path = _resolve_within_root(root_path, dir_path)
            if abs_path is None:
                return {"error": "Path outside of allowed root directory"}

            os.makedirs(abs_path, exist_ok=True)
            
            return {
                "content": [{
                    "type": "text",
                    "text": f"Successfully created directory {dir_path}"
                }]
            }
        except Exception as e:
            return {"error": f"Failed to create directory: {str(e)}"}
    
    def get_server_info(self) -> Dict[str, Any]:
        """Get information about all MCP servers (built-in + external)"""
        info = {
            "builtin_servers": {},
            "external_servers": {}
        }
        
        # Built-in servers
        for server_name, server_info in self.mcp_servers.items():
            info["builtin_servers"][server_name] = {
                "tools": server_info["tools"],
                "server": server_name,
                "type": server_info["type"],
                "root_path": server_info.get("root_path", "")
            }
        
        # External servers
        if self.external_proxy:
            info["external_servers"] = self.external_proxy.get_server_status()
        
        return info


class OneStepPixiClient:
    """Enhanced client with full feature parity + MCP capabilities + HTTP Proxy + FIXED Environment Injection + OFFLINE DEPLOYMENT + AUTO-APPROVE"""
    
    def __init__(self, server_url: str, aes_key_file: Optional[str] = None, 
                 auth_headers: Optional[Dict[str, str]] = None, handshake_secret: Optional[str] = None,
                 env_profile: Optional[str] = None, auto_approve: bool = False):
        self.server_url = server_url
        self.aes_key_file = aes_key_file
        self.auth_headers = auth_headers or {}
        self.handshake_secret = handshake_secret
        self.env_profile = env_profile
        self.auto_approve = auto_approve
        self.mcp_handler = None
        self.environment_handler = None
        self.proxy_handler = None  # NEW: HTTP proxy handler
        self.local_proxy_server = None  # NEW: Local HTTP proxy server for testing
        self.task_id = None
        self.aes_key_bytes = None
        self._encryption_setup_done = False
        self._processed_mcp_requests = OrderedDict()  # LRU of processed MCP request IDs
        self._processed_proxy_requests = OrderedDict()  # LRU of processed proxy request IDs
        self._should_auto_exit = False  # Flag for auto-exit signaling

    _DEDUP_CACHE_SIZE = 1024

    def _is_duplicate_request(self, cache: OrderedDict, request_id: Optional[str]) -> bool:
        """Dedup by request_id with a bounded LRU; requests without an ID are never deduped."""
        if not request_id:
            return False
        if request_id in cache:
            cache.move_to_end(request_id)
            return True
        cache[request_id] = True
        if len(cache) > self._DEDUP_CACHE_SIZE:
            cache.popitem(last=False)
        return False
        
    def _setup_encryption(self) -> bool:
        """Setup AES encryption via handshake or key file (only once)"""
        if self._encryption_setup_done:
            print("🔐 Encryption already configured")
            return True

        print("📋 Step 0: Setting up encryption...")

        # Check if encryption was already set up in main() via transport.aes_key
        if transport.aes_key is not None:
            print("✅ Using AES key from previous handshake/setup")
            self.aes_key_bytes = transport.aes_key
            self._encryption_setup_done = True
            return True

        # Option 1: Handshake (preferred) - only if not already done
        if self.handshake_secret:
            try:
                self.aes_key_bytes, _ = perform_handshake(self.handshake_secret, rotate=False)
                transport.aes_key = self.aes_key_bytes  # Update shared state
                print("✅ AES key obtained via handshake")
                self._encryption_setup_done = True
                return True
            except Exception as e:
                print(f"❌ Handshake failed: {e}")
                return False

        # Option 2: Key file
        if self.aes_key_file:
            try:
                raw = Path(self.aes_key_file).read_bytes()
                try:
                    cand = base64.b64decode(raw.strip(), validate=True)
                    self.aes_key_bytes = cand if len(cand) == 32 else raw
                except Exception:
                    self.aes_key_bytes = raw
                if len(self.aes_key_bytes) != 32:
                    raise ValueError(f"Invalid AES key length: {len(self.aes_key_bytes)}")

                transport.aes_key = self.aes_key_bytes  # Update shared state
                print("✅ AES key loaded from file")
                self._encryption_setup_done = True
                return True
            except Exception as e:
                print(f"❌ Failed to load AES key: {e}")
                return False
        
        # Option 3: No encryption
        print("⚠️ No encryption configured - communication will be unencrypted")
        self._encryption_setup_done = True
        return True
    
    def deploy_task_with_full_features(self, task_name: str, full_config: Dict[str, Any], offline_mode: bool = False) -> str:
        """Deploy task with MCP + Proxy + Environment + Offline + Auto-approve support"""
        # Update shared state for signal handler compatibility
        transport.server_url = self.server_url
        transport.auth_headers = self.auth_headers

        # Step 0: Setup encryption (only once)
        if not self._setup_encryption():
            raise Exception("Failed to setup encryption")
        
        # Step 1: Start MCP servers (both built-in and external)
        mcp_config = full_config.get("mcp", {})
        print("📋 Step 1: Starting local MCP servers...")
        self.mcp_handler = MCPBackChannelHandler()
        
        # Configure external MCP servers first
        self.mcp_handler.configure_external_servers(full_config)
        
        # Then configure built-in filesystem server if enabled
        if mcp_config.get("filesystem", {}).get("enabled"):
            root_path = mcp_config["filesystem"]["root_path"]
            self.mcp_handler.register_filesystem_server("filesystem", root_path)
            print(f"✅ Built-in filesystem server: {root_path}")
        
        # Show MCP server summary
        server_info = self.mcp_handler.get_server_info()
        external_count = len(server_info.get("external_servers", {}).get("servers", []))
        builtin_count = len(server_info.get("builtin_servers", {}))
        print(f"🔧 MCP Configuration: {external_count} external + {builtin_count} built-in servers")
        
        # Step 1.5: Setup HTTP Proxy
        proxy_config = full_config.get("proxy", {})
        if proxy_config.get("enabled"):
            print("📋 Step 1.5: Setting up HTTP(S) reverse proxy...")
            client_info = {
                "ip": self._get_client_ip(),
                "proxy_port": proxy_config.get("remote_port", 8080),
                "hostname": os.uname().nodename if hasattr(os, 'uname') else "unknown"
            }
            
            self.proxy_handler = ProxyRequestHandler(full_config, client_info)
            proxy_port = proxy_config.get("remote_port", 8080)
            print(f"🌐 HTTP Proxy configured for remote port {proxy_port}")
            print(f"📍 Loaded {len(self.proxy_handler.mappings)} URL mappings")
            
            # NEW: Start local proxy test server based on config (with CLI override support)
            enable_test_server = self._should_enable_test_server(proxy_config)
            if enable_test_server:
                self.local_proxy_server = LocalProxyTestServer(self.proxy_handler, proxy_port)
                if self.local_proxy_server.start():
                    print(f"🧪 Local test server started on http://localhost:{proxy_port}")
                    print(f"💡 You can test directly: curl http://localhost:{proxy_port}/api/health")
                else:
                    print(f"⚠️  Failed to start local test server on port {proxy_port}")
                    self.local_proxy_server = None
            else:
                print(f"⏭️  Local test server disabled (config: enable_local_test_server = false)")
            
            # Show quick test commands for immediate use
            if self.proxy_handler.mappings:
                print(f"\n🧪 Quick Test Commands:")
                for mapping in self.proxy_handler.mappings[:3]:  # Show first 3 mappings
                    remote_path = mapping.get("remote_path", "/*")
                    if remote_path.endswith("/*"):
                        base_path = remote_path[:-2]
                        print(f"   curl http://localhost:{proxy_port}{base_path}/health")
                        break
                    elif remote_path == "/*":
                        print(f"   curl http://localhost:{proxy_port}/")
                        break
                    else:
                        print(f"   curl http://localhost:{proxy_port}{remote_path}")
                        break
                if len(self.proxy_handler.mappings) > 1:
                    print(f"   (+ {len(self.proxy_handler.mappings) - 1} more mappings available)")
                print()
        
        # Step 2: Create MCP configuration (now includes proxy info)
        print("📋 Step 2: Creating MCP configuration for remote task...")
        mcp_info = self._create_mcp_config(full_config)
        
        with open("mcp_info.json", "w") as f:
            json.dump(mcp_info, f, indent=2)
        print("  ✅ MCP configuration saved to mcp_info.json")
        
        # Step 2.5: FIXED - Environment Variable Injection Setup  
        print("📋 Step 2.5: Setting up environment variable injection...")
        client_info = {
            "ip": self._get_client_ip(),
            "proxy_port": proxy_config.get("remote_port", 8080),
            "hostname": os.uname().nodename if hasattr(os, 'uname') else "unknown"
        }
        
        # Generate temporary task ID for environment setup
        temp_task_id = f"{task_name}-{int(time.time())}"
        self.environment_handler = EnvironmentHandler(full_config, temp_task_id, client_info)
        
        if self.environment_handler.is_enabled():
            env_vars = self.environment_handler.get_environment_variables(self.env_profile)
            
            print(f"  ✅ Environment injection prepared: {len(env_vars)} variables")
            
            # Show preview of injected variables (for debugging)
            print("  🌍 Environment variables to inject:")
            for key, value in list(env_vars.items())[:5]:  # Show first 5
                # Hide sensitive values
                if any(secret in key.lower() for secret in ['password', 'secret', 'key', 'token']):
                    display_value = "***"
                elif len(value) < 50:
                    display_value = value
                else:
                    display_value = f"{value[:10]}..."
                print(f"    {key}={display_value}")
            if len(env_vars) > 5:
                print(f"    ... and {len(env_vars) - 5} more variables")
        else:
            print("  ⏭️ Environment injection disabled")
        
        # Step 3: ENHANCED - Deploy task with offline support
        deployment_mode = "offline" if offline_mode else "online"
        auto_approve_status = " + auto-approve" if self.auto_approve else ""
        print(f"📋 Step 3: Deploying task to remote server ({deployment_mode} mode{auto_approve_status})...")
        
        # Capture task ID during streaming via callback
        task_id_holder = {"task_id": None}

        def capture_task_id(obj: dict):
            if obj.get("task_id"):
                task_id_holder["task_id"] = obj["task_id"]
                self.task_id = obj["task_id"]
                # Update environment handler with real task ID
                if self.environment_handler:
                    self.environment_handler.task_id = obj["task_id"]
            _handle_obj(obj)

        # ENHANCED: Pass offline mode and auto-approve to packaging function
        pkg = _package_dir(env_handler=self.environment_handler,
                          env_profile=self.env_profile,
                          offline_mode=offline_mode,
                          auto_approve=self.auto_approve)
        _upload_and_run(pkg, task_name, self.auth_headers, self.environment_handler,
                        on_obj=capture_task_id)

        # Use captured task ID or fallback
        self.task_id = task_id_holder["task_id"] or transport.task_id or f"{task_name}-{int(time.time())}"
        
        # Step 4: Start MCP + Proxy listener with automation support
        print("📋 Step 4: Starting MCP back-channel + HTTP proxy listener...")
        self.automation_config = mcp_config.get("automation", {})
        self._start_enhanced_listener()
        
        # NEW: Show proxy testing information after successful setup
        if self.proxy_handler and self.proxy_handler.is_enabled():
            self._display_proxy_test_info()
        
        return self.task_id
    
    def _should_enable_test_server(self, proxy_config: Dict[str, Any]) -> bool:
        """Determine if the local test server should be enabled based on config and CLI args"""
        # This method will be called with the final proxy config that includes CLI overrides
        # The logic in main() handles the precedence: CLI > config > default
        return proxy_config.get("enable_local_test_server", True)
    
    def deploy_task_standard(self, task_name: str, full_config: Dict[str, Any], offline_mode: bool = False) -> str:
        """Deploy task without MCP/Proxy but with environment injection, offline support, and auto-approve"""
        # Update shared state for signal handler compatibility
        transport.server_url = self.server_url
        transport.auth_headers = self.auth_headers

        # Setup encryption (only once)
        if not self._setup_encryption():
            raise Exception("Failed to setup encryption")
        
        # Setup environment handler for standard deployment too
        deployment_mode = "offline" if offline_mode else "online"
        auto_approve_status = " + auto-approve" if self.auto_approve else ""
        print(f"💻 Running standard deployment ({deployment_mode} mode{auto_approve_status})...")
        client_info = {
            "ip": self._get_client_ip(),
            "proxy_port": full_config.get("proxy", {}).get("remote_port", 8080),
            "hostname": os.uname().nodename if hasattr(os, 'uname') else "unknown"
        }
        
        temp_task_id = f"{task_name}-{int(time.time())}"
        self.environment_handler = EnvironmentHandler(full_config, temp_task_id, client_info)
        
        if self.environment_handler.is_enabled():
            print("🌍 Environment injection enabled for standard deployment")
            env_vars = self.environment_handler.get_environment_variables(self.env_profile)
            print(f"✅ Will package {len(env_vars)} environment variables")
        
        # Deploy task using enhanced logic with offline mode and auto-approve support
        pkg = _package_dir(env_handler=self.environment_handler, 
                          env_profile=self.env_profile,
                          offline_mode=offline_mode,
                          auto_approve=self.auto_approve)
        _upload_and_run(pkg, task_name, self.auth_headers, self.environment_handler)

        self.task_id = transport.task_id
        return self.task_id or f"{task_name}-{int(time.time())}"
    
    def _create_mcp_config(self, full_config: Dict[str, Any]) -> Dict[str, Any]:
        """Create MCP configuration for remote task (includes built-in + external servers)"""
        encryption_enabled = self.aes_key_bytes is not None
        
        config = {
            "mcp_enabled": bool(self.mcp_handler),
            "encryption_enabled": encryption_enabled,
            "back_channel_url": f"{self.server_url}/mcp/{self.task_id}",
        }
        
        # Add comprehensive MCP server info
        if self.mcp_handler:
            server_info = self.mcp_handler.get_server_info()
            config["servers"] = server_info
            
            # Add summary counts
            builtin_count = len(server_info.get("builtin_servers", {}))
            external_info = server_info.get("external_servers", {})
            external_count = len(external_info.get("servers", []))
            
            config["server_summary"] = {
                "builtin_servers": builtin_count,
                "external_servers": external_count,
                "total_servers": builtin_count + external_count,
                "external_routing_enabled": external_info.get("enabled", False)
            }
            
            print(f"🔧 MCP Configuration: {external_count} external + {builtin_count} built-in servers")
        
        # Add proxy configuration
        proxy_config = full_config.get("proxy", {})
        if proxy_config.get("enabled") and self.proxy_handler:
            config["proxy_enabled"] = True
            config["proxy_port"] = proxy_config.get("remote_port", 8080)
            config["proxy_mappings"] = len(self.proxy_handler.mappings)
            print(f"🌐 HTTP Proxy enabled on remote port {config['proxy_port']}")
        else:
            config["proxy_enabled"] = False
        
        if encryption_enabled:
            print(f"🔐 MCP + Proxy back-channel will use AES-256 encryption")
        else:
            print(f"⚠️ MCP + Proxy back-channel will be UNENCRYPTED (no AES key available)")
        
        return config
    
    def _start_enhanced_listener(self):
        """Start enhanced listener for both MCP and HTTP proxy requests"""
        def enhanced_listener():
            asyncio.run(self._enhanced_listener_async())
        
        listener_thread = threading.Thread(target=enhanced_listener, daemon=True)
        listener_thread.start()
        print("✅ Enhanced MCP + HTTP Proxy listener started")
    
    async def _enhanced_listener_async(self):
        """Async listener that handles both MCP and HTTP proxy requests - FIXED AUTO-EXIT"""
        if not self.task_id:
            print("❌ No task ID available for enhanced listener")
            return
            
        print(f"👂 Enhanced listener monitoring task {self.task_id} output...")
        
        # Perform health check on external MCP servers if configured
        if self.mcp_handler and self.mcp_handler.external_proxy:
            print("🏥 Performing initial health check on external MCP servers...")
            try:
                await self.mcp_handler.external_proxy.health_check_servers()
            except Exception as e:
                print(f"⚠️ Health check failed: {e}")
        
        processed_output_count = 0
        last_activity = time.time()  # Track when we last saw any activity
        operations_started = False   # Track if we've seen any operations
        
        while True:
            try:
                # Get task output to look for requests
                response = _http("GET", f"{self.server_url}/task/{self.task_id}", headers=self.auth_headers)
                if response.status_code == 200:
                    task_info = response.json()
                    
                    # Check if task is completed
                    task_status = task_info.get("status", "unknown")
                    recent_output = task_info.get("recent_output", [])
                    
                    # Process any NEW output lines we haven't seen before
                    if len(recent_output) > processed_output_count:
                        new_output_lines = recent_output[processed_output_count:]
                        
                        for output_item in new_output_lines:
                            if output_item.get("type") == "output":
                                content = output_item.get("content", "")
                                
                                # Check for MCP or HTTP proxy requests
                                if content.strip().startswith("MCP_REQUEST:") or content.strip().startswith("HTTP_PROXY:"):
                                    operations_started = True
                                    last_activity = time.time()
                                    request_type = "MCP" if content.strip().startswith("MCP_REQUEST:") else "HTTP"
                                    print(f"🔧 {request_type} activity detected at {time.strftime('%H:%M:%S')}")
                                
                                await self._process_enhanced_output_line(content)
                        
                        # Update our processed count
                        processed_output_count = len(recent_output)
                    
                    # FIXED AUTO-EXIT LOGIC
                    automation = self.automation_config
                    if automation.get("mode") == "auto_exit":
                        max_wait = automation.get("max_wait_seconds", 120)
                        idle_threshold = 5  # seconds of no activity
                        
                        current_time = time.time()
                        time_since_last_activity = current_time - last_activity
                        
                        # Exit conditions:
                        if task_status in ["completed", "failed"]:
                            if operations_started and time_since_last_activity > idle_threshold:
                                print(f"✅ Task {task_status} and no activity for {idle_threshold}s - auto-exiting")
                                break
                            elif not operations_started:
                                print(f"✅ Task {task_status} with no operations - auto-exiting")
                                break
                        elif operations_started and time_since_last_activity > max_wait:
                            print(f"⏰ No activity for {max_wait}s - auto-exiting")
                            break
                    
                elif response.status_code == 404:
                    print(f"🏁 Task {self.task_id} not found, stopping enhanced listener")
                    break
                
                await asyncio.sleep(0.5)  # Check every 500ms
                
            except Exception as e:
                print(f"❌ Enhanced listener error: {e}")
                await asyncio.sleep(5)
        
        print("👋 Enhanced listener stopped - triggering auto-exit")
        # Set a flag that main() can check
        self._should_auto_exit = True
    
    async def _process_enhanced_output_line(self, line: str):
        """Process a single output line for both MCP and HTTP proxy requests"""
        # Handle MCP requests
        if line.strip().startswith("MCP_REQUEST:"):
            await self._process_mcp_output_line(line)
        
        # Handle HTTP proxy requests
        elif line.strip().startswith("HTTP_PROXY:"):
            await self._process_proxy_output_line(line)
    
    async def _process_mcp_output_line(self, line: str):
        """Process a single output line for MCP requests (existing logic)"""
        if not line.strip().startswith("MCP_REQUEST:"):
            return
            
        try:
            # Extract JSON from MCP_REQUEST: prefix
            json_str = line.strip()[12:]  # Remove "MCP_REQUEST:" prefix
            request = json.loads(json_str)
            
            encryption_status = "🔐 encrypted" if self.aes_key_bytes else "⚠️ unencrypted"
            print(f"🔍 Found MCP request ({encryption_status}): {json_str[:100]}...")

            # Check if we've already processed this request (by request_id)
            if self._is_duplicate_request(self._processed_mcp_requests, request.get("request_id")):
                print(f"🔄 Skipping duplicate MCP request (request_id: {request['request_id']})")
                return  # Skip silently - already processed

            print(f"🔧 Processing MCP request: {request.get('action', 'unknown')}")

            # Generate a request ID for tracking if not present
            if "request_id" not in request:
                request["request_id"] = f"client_generated_{int(time.time() * 1000)}"
            
            # Handle the MCP request
            response = await self._handle_mcp_request(request)
            print(f"🔧 MCP response generated: {response}")
            
            # Send encrypted response back to task
            await self._send_backchannel_response("mcp", response)
            
        except json.JSONDecodeError as e:
            print(f"❌ Invalid MCP request JSON: {e}")
        except Exception as e:
            print(f"❌ MCP request processing error: {e}")
            import traceback
            traceback.print_exc()  # Full error trace
    
    async def _process_proxy_output_line(self, line: str):
        """Process a single output line for HTTP proxy requests"""
        if not line.strip().startswith("HTTP_PROXY:"):
            return
            
        try:
            # Extract JSON from HTTP_PROXY: prefix
            json_str = line.strip()[11:]  # Remove "HTTP_PROXY:" prefix
            request = json.loads(json_str)
            
            encryption_status = "🔐 encrypted" if self.aes_key_bytes else "⚠️ unencrypted"
            print(f"🌐 Found HTTP proxy request ({encryption_status}): {json_str[:100]}...")

            # Check if we've already processed this request (by request_id)
            if self._is_duplicate_request(self._processed_proxy_requests, request.get("request_id")):
                print(f"🔄 Skipping duplicate HTTP proxy request (request_id: {request['request_id']})")
                return  # Skip silently - already processed

            method = request.get("method", "GET")
            path = request.get("path", "/")
            print(f"🌐 Processing HTTP proxy request: {method} {path}")

            # Generate a request ID for tracking if not present
            if "request_id" not in request:
                request["request_id"] = f"proxy_{int(time.time() * 1000)}"
            
            # Handle the proxy request
            if self.proxy_handler and self.proxy_handler.is_enabled():
                response = self.proxy_handler.process_request(request)
                response["request_id"] = request.get("request_id")
                print(f"🌐 HTTP proxy response: {response.get('status', 'unknown')}")
            else:
                response = {
                    "request_id": request.get("request_id"),
                    "status": 503,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": "HTTP proxy not enabled or configured"})
                }
            
            # Send response back to task
            await self._send_backchannel_response("proxy", response)
            
        except json.JSONDecodeError as e:
            print(f"❌ Invalid HTTP proxy request JSON: {e}")
        except Exception as e:
            print(f"❌ HTTP proxy request processing error: {e}")
            import traceback
            traceback.print_exc()  # Full error trace
    
    async def _handle_mcp_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Enhanced MCP request handler with external server routing"""
        action = request.get("action", "")
        request_id = request.get("request_id", str(uuid.uuid4()))
        
        try:
            if not self.mcp_handler:
                return {"error": "MCP handler not available", "request_id": request_id}
            
            print(f"🔧 Processing MCP request: {action}")
            
            # Use the enhanced routing system
            result = await self.mcp_handler.route_request({
                "action": action,
                "params": request.get("params", {}),
                "request_id": request_id
            })
            
            print(f"✅ MCP request completed: {action}")
            return result
                
        except Exception as e:
            print(f"❌ MCP handler error for {action}: {e}")
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "request_id": request_id,
                "error": f"MCP handler error: {str(e)}"
            }
    
    async def _send_backchannel_response(self, kind: str, response: Dict[str, Any]):
        """Send MCP or HTTP proxy response back to remote task via encrypted channel"""
        label, response_type = {
            "mcp": ("MCP", "mcp_response"),
            "proxy": ("HTTP proxy", "http_response"),
        }[kind]

        try:
            # First check if the task is still running
            task_check = _http("GET", f"{self.server_url}/task/{self.task_id}", headers=self.auth_headers)
            if task_check.status_code == 200:
                task_info = task_check.json()
                task_status = task_info.get("status", "unknown")

                if task_status in ["completed", "failed"]:
                    print(f"📤 Task already {task_status}, skipping {label} response send")
                    return
            elif task_check.status_code == 404:
                print(f"📤 Task not found, skipping {label} response send")
                return

            # ENCRYPT RESPONSE if encryption is enabled
            response_data = {
                "type": response_type,
                "data": response
            }

            # Serialize the response
            response_json = json.dumps(response_data)

            if self.aes_key_bytes:
                # ENCRYPT the response using the same AES key as other communications
                print(f"🔐 Encrypting {label} response with AES-256...")

                # Use the same encryption format as the rest of the system
                nonce = os.urandom(NONCE_LEN)
                aesgcm = AESGCM(self.aes_key_bytes)
                encrypted_data = aesgcm.encrypt(nonce, response_json.encode('utf-8'), None)

                # Combine nonce + encrypted data (same format as streaming)
                encrypted_payload = nonce + encrypted_data

                # Send encrypted payload
                response_obj = _http(
                    "POST",
                    f"{self.server_url}/task/{self.task_id}/input",
                    headers={**self.auth_headers, "Content-Type": "application/octet-stream"},
                    data=encrypted_payload,
                    timeout=10
                )

                print(f"🔐 {label} response encrypted and sent ({len(encrypted_payload)} bytes)")
            else:
                # Send unencrypted (fallback for when no encryption is configured)
                print(f"⚠️ Sending UNENCRYPTED {label} response (no AES key available)")

                response_obj = _http(
                    "POST",
                    f"{self.server_url}/task/{self.task_id}/input",
                    headers={**self.auth_headers, "Content-Type": "application/json"},
                    json=response_data,
                    timeout=10
                )

            if response_obj.status_code == 200:
                server_response = response_obj.json() if response_obj.text else {}
                encryption_status = "🔐 encrypted" if self.aes_key_bytes else "⚠️ unencrypted"
                print(f"✅ {label} response sent successfully ({encryption_status}): {server_response}")
            else:
                print(f"❌ Server rejected {label} response: {response_obj.status_code} - {response_obj.text}")

            if kind == "mcp":
                print(f"✅ Sent MCP response for action: {response.get('action', 'unknown')}")
            else:
                print(f"✅ Sent HTTP proxy response: {response.get('status', 'unknown')}")

        except requests.exceptions.Timeout:
            print(f"❌ Timeout sending {label} response")
        except requests.exceptions.RequestException as e:
            print(f"❌ Request error sending {label} response: {e}")
        except Exception as e:
            print(f"❌ Failed to send {label} response: {e}")
            import traceback
            traceback.print_exc()
    
    def _get_client_ip(self) -> str:
        """Get client's IP address"""
        try:
            # Connect to a remote address to get local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
    
    def keep_alive(self):
        """Keep the client running to maintain MCP + Proxy back-channel"""
        print("\n⏳ MCP back-channel + HTTP Proxy active. Remote task can access local services.")
        if self.local_proxy_server:
            proxy_port = self.local_proxy_server.port
            print(f"🧪 Local test server running on http://localhost:{proxy_port}")
            print(f"💡 Test with: curl http://localhost:{proxy_port}/api/health")
        print("Press Ctrl+C for options menu.")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Stopping MCP back-channel + HTTP Proxy...")
            if self.local_proxy_server:
                self.local_proxy_server.stop()
            print("👋 Enhanced back-channel stopped.")
    
    def _display_proxy_test_info(self):
        """Display helpful proxy testing information"""
        if not self.proxy_handler or not self.proxy_handler.is_enabled():
            return
            
        mapping_info = self.proxy_handler.get_mapping_info()
        remote_port = mapping_info["remote_port"]
        
        print("\n" + "="*70)
        print("🌐 HTTP PROXY READY FOR TESTING")
        print("="*70)
        print(f"📍 Remote Proxy Port: {remote_port}")
        print(f"🔒 Encryption: {'AES-256' if self.aes_key_bytes else 'None (Plain Text)'}")
        print(f"📡 Tunnel: Remote Task ↔ Encrypted Channel ↔ Local Services")
        print()
        
        if mapping_info["mappings"]:
            print("🎯 Available Endpoints:")
            for mapping in mapping_info["mappings"]:
                name = mapping["name"]
                remote_url = mapping["remote_url"] 
                local_url = mapping["local_url"]
                
                print(f"  📌 {name}")
                print(f"     Remote: {remote_url}")
                print(f"     Local:  {local_url}")
                
                # Show transformations if any
                trans = mapping["transformations"]
                if trans["add_headers"] or trans["remove_headers"] or trans["replace_headers"]:
                    transforms = []
                    if trans["add_headers"]: transforms.append(f"+{trans['add_headers']} headers")
                    if trans["remove_headers"]: transforms.append(f"-{trans['remove_headers']} headers")
                    if trans["replace_headers"]: transforms.append(f"~{trans['replace_headers']} headers")
                    print(f"     Transform: {', '.join(transforms)}")
                
                print()
            
            print(f"🧪 Copy-Paste Test Commands:")
            if self.local_proxy_server:
                print(f"   # Test locally (bypasses remote task):")
                print(f"   curl http://localhost:{remote_port}/api/health")
                print(f"   curl http://localhost:{remote_port}/api/status")
                print()
                print(f"   # Test from remote task (through encrypted tunnel):")
                print(f"   # Deploy a task that makes requests to localhost:{remote_port}")
            else:
                print(f"   # Test from your remote task environment:")
            
            # Show practical test commands
            for mapping in mapping_info["mappings"][:2]:  # Show first 2 mappings
                test_urls = mapping["test_urls"]
                for url in test_urls[:2]:  # Show first 2 URLs per mapping
                    if not self.local_proxy_server:
                        print(f"   curl {url}")
                    
            print()
            if self.local_proxy_server:
                print("   # Test POST request (local):")
            else:
                print("   # Test POST request:")
            first_mapping = mapping_info["mappings"][0]
            remote_path = first_mapping["remote_path"]
            if remote_path.endswith("/*"):
                base_path = remote_path[:-2]
                print(f"   curl -X POST -H 'Content-Type: application/json' \\")
                print(f"        -d '{{\"message\":\"Hello from remote task!\"}}' \\")
                print(f"        http://localhost:{remote_port}{base_path}/echo")
            else:
                print(f"   curl -X POST -H 'Content-Type: application/json' \\")
                print(f"        -d '{{\"test\":\"data\"}}' \\")
                print(f"        http://localhost:{remote_port}/api/data")
        
        print()
        print("💡 Tips:")
        if self.local_proxy_server:
            print(f"   • Local test server running on localhost:{remote_port} for immediate testing")
            print(f"   • Remote task proxy also available on localhost:{remote_port} (inside task environment)")
        else:
            print("   • Remote task proxy available on localhost:{} (inside task environment)".format(remote_port))
        print("   • All requests to localhost:{} will be proxied through encrypted tunnel".format(remote_port))
        print("   • Headers are automatically transformed (auth tokens injected)")
        print("   • Local services never see remote client details")
        print("   • Use --show-proxy to see detailed mapping configuration")
        print("="*70)
        print()


def main():
    """Enhanced main function with full feature parity + HTTP Proxy + FIXED AUTO-EXIT + FIXED Environment Injection + Config-based Test Server Control + OFFLINE DEPLOYMENT + AUTO-APPROVE"""
    aes_key: Optional[bytes] = None

    # Load configuration from pixi_remote_config.toml (matching original)
    cfg = load_pixi_config()
    
    parser = argparse.ArgumentParser(description="Enhanced Pixi Remote Client with MCP + HTTP Proxy + Environment Injection + Offline Deployment + Auto-Approve")
    
    # Core arguments (matching original client.py)
    # Do not set a default here; we resolve precedence after parsing
    parser.add_argument("--server", default=None, help="Pixi server URL")
    parser.add_argument("--server-from-token", action="store_true",
                       help="Allow deriving the server URL from bearer token claims (off by default)")
    parser.add_argument("--task", default="foobar", help="Task name to deploy")  
    parser.add_argument("--attach", help="Attach to existing task ID")
    parser.add_argument("--attach-history", help="Attach to task with full history")
    # Leave tokens unset by default; resolve via precedence (CLI > env > config)
    parser.add_argument("--bearer-token", default=None, help="Bearer token")
    parser.add_argument("--snowflake-token", default=None, help="Snowflake token")
    parser.add_argument("--header", action="append", metavar="'Key: Value'",
                       help="Extra HTTP header to send on every request (repeatable). "
                            "Values support ${env:VAR} and ${file:path}.")
    parser.add_argument("--headers-file", default=None,
                       help=f"JSON file of headers to send (default: {DEFAULT_HEADERS_FILE} if present)")
    parser.add_argument("--show-headers", action="store_true",
                       help="Show the custom headers that would be sent (secrets masked) and exit")
    parser.add_argument("--aes-key", default="aes.key" if Path("aes.key").exists() else None,
                       help="Path to base64 AES key file")
    
    # Handshake support (matching original)
    parser.add_argument("--handshake-secret", default=cfg.get("handshake-secret") or cfg.get("handshake_secret"), 
                       help="Do handshake then run/attach")
    parser.add_argument("--rotate-secret", help="Rotate AES key via handshake")
    
    # MCP configuration (enhanced features with external server support)
    parser.add_argument("--with-mcp", action="store_true", help="Enable MCP back-channel")
    parser.add_argument("--mcp-filesystem", action="store_true", help="Enable built-in MCP filesystem")
    parser.add_argument("--mcp-root", help="Root path for built-in MCP filesystem")
    parser.add_argument("--with-external-mcp", action="store_true", help="Enable external MCP server routing")
    parser.add_argument("--show-mcp", action="store_true", help="Show MCP server configuration and exit")
    
    # NEW: HTTP Proxy configuration with enhanced test server control
    parser.add_argument("--with-proxy", action="store_true", help="Enable HTTP(S) reverse proxy")
    parser.add_argument("--proxy-port", type=int, help="Remote proxy port (overrides config)")
    parser.add_argument("--no-local-test-server", action="store_true", help="Disable local proxy test server (overrides config)")
    parser.add_argument("--local-test-server", action="store_true", help="Force enable local proxy test server (overrides config)")
    
    # NEW: Offline deployment options
    parser.add_argument("--offline-mode", action="store_true", help="Package dependencies for offline deployment")
    parser.add_argument("--validate-dependencies", action="store_true", help="Validate .pixi folder before packaging") 
    parser.add_argument("--show-package-size", action="store_true", help="Show package size breakdown")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve confirmations (overrides config)")
    
    # Execution mode
    parser.add_argument("--keep-alive", action="store_true", help="Keep back-channel alive after deployment")
    parser.add_argument("--show-config", action="store_true", help="Show current configuration and exit")
    
    # New automated options
    parser.add_argument("--timeout", type=int, help="Seconds to keep back-channel alive after task completion")
    parser.add_argument("--auto-exit", action="store_true", help="Automatically exit when operations complete")
    parser.add_argument("--skip-offline-check", action="store_true", help="Skip preflight server reachability check")
    
    # New environment injection options
    parser.add_argument("--env-profile", help="Environment profile to use (dev, prod, etc.)")
    parser.add_argument("--show-env", action="store_true", help="Show environment variables that would be injected")
    
    # NEW: Proxy information options
    parser.add_argument("--show-proxy", action="store_true", help="Show HTTP proxy URL mappings and exit")
    parser.add_argument("--help-proxy", action="store_true", help="Show detailed HTTP proxy help")
    
    
    args = parser.parse_args()

    # Resolve server URL with precedence: CLI > env > config > token claim (opt-in)
    server_url_resolved, server_source = _resolve_server_url(args.server, cfg, args.bearer_token,
                                                             allow_token_url=args.server_from_token)
    server_url = server_url_resolved
    transport.server_url = server_url

    # Quick preflight: validate URL and check TCP reachability before doing anything heavy
    # Allow --show-config, --show-package-size or --skip-offline-check to bypass reachability requirement
    if not (args.show_config or args.show_package_size or args.skip_offline_check):
        if not _is_valid_url(server_url):
            print(f"❌ Invalid server URL: {server_url}")
            print("💡 Set a valid URL via --server or PIXI_SERVER_URL")
            sys.exit(2)

        ok, err = _quick_check_server_online(server_url, timeout=1.5)
        if not ok:
            print(f"❌ Server appears offline or unreachable: {server_url}")
            if err:
                print(f"   Reason: {err}")
            print("💡 Ensure the server is running or specify the correct --server URL")
            sys.exit(2)


    # Resolve auth tokens with precedence: CLI > env > config
    def _pick_token(cli_val: Optional[str], env_vars: List[str], cfg_keys: List[str]) -> (Optional[str], str):
        if cli_val and cli_val.strip():
            return cli_val.strip(), "CLI"
        for ev in env_vars:
            v = os.getenv(ev)
            if v and v.strip():
                return v.strip(), f"env:{ev}"
        for ck in cfg_keys:
            v = cfg.get(ck)
            if v and isinstance(v, str) and v.strip():
                return v.strip(), f"config:{ck}"
        return None, "unset"

    bearer_token, bearer_src = _pick_token(
        args.bearer_token,
        ["BEARER_TOKEN", "PIXI_BEARER_TOKEN", "AUTH_TOKEN"],
        ["bearer_token", "bearer-token"]
    )
    snowflake_token, snowflake_src = _pick_token(
        args.snowflake_token,
        ["SNOWFLAKE_TOKEN"],
        ["snowflake_token", "snowflake-token"]
    )

    _auth_headers = _hdrs(bearer_token, snowflake_token)
    transport.auth_headers = _auth_headers

    # Arbitrary custom headers (config < file < CLI), applied to every request.
    _custom_headers = build_custom_headers(args.header, args.headers_file, cfg.get("headers", {}))
    if args.show_headers:
        if _custom_headers:
            print("📨 Custom request headers:")
            for _k, _v in _custom_headers.items():
                print(f"  {_k}: {mask_header_value(_k, _v)}")
        else:
            print("📨 No custom request headers configured.")
        return
    set_default_headers(_custom_headers)

    if args.help_proxy:
        print("🌐 HTTP Proxy Help")
        print("=" * 50)
        print()
        print("The HTTP proxy creates a secure tunnel that allows remote tasks")
        print("to access your local services through encrypted channels.")
        print()
        print("🏗️ Architecture:")
        print("  Remote Task → HTTP Proxy (port 8081) → Encrypted Tunnel → Client → Local Service (port 5000)")
        print()
        print("🧪 Testing Methods:")
        print("  1. Local Test Server (for development):")
        print("     • Runs on your local machine")
        print("     • Simulates the remote proxy behavior")
        print("     • Test with: curl http://localhost:8081/api/health")
        print("     • Controlled by config: enable_local_test_server = true/false")
        print("     • CLI overrides: --local-test-server / --no-local-test-server")
        print()
        print("  2. Remote Task Testing (production):")
        print("     • Deploy a task that makes HTTP requests")
        print("     • Requests go through encrypted tunnel")
        print("     • Test from within remote task environment")
        print()
        print("⚙️ Configuration:")
        print("  --with-proxy              Enable HTTP proxy")
        print("  --proxy-port 8080         Set remote proxy port")
        print("  --local-test-server       Force enable local test server")
        print("  --no-local-test-server    Force disable local test server")
        print("  --show-proxy              Show proxy mappings")
        print()
        print("📋 Configuration File (pixi_remote_config.toml):")
        print("  [config.proxy]")
        print("  enabled = true")
        print("  enable_local_test_server = true  # Can be overridden by CLI")
        print("  remote_port = 8080")
        print()
        print("🔧 Precedence:")
        print("  CLI Arguments > Config File > Defaults")
        print("  --local-test-server       → Always enable (highest priority)")
        print("  --no-local-test-server    → Always disable")
        print("  config.proxy.enable_local_test_server → Use config setting")
        print("  (no setting)              → Default to enabled")
        print()
        print("📋 Example:")
        print("  python enhanced_client.py --task my_task --with-proxy --keep-alive")
        print()
        return 0
    
    # Determine auto-approve setting with precedence: CLI > config > default
    auto_approve = get_auto_approve_setting(args.auto_approve, cfg)
    if not auto_approve and not args.auto_approve:
        print("🤚 Auto-approve: disabled (interactive mode)")
    
    if args.show_config:
        print("📋 Current Configuration (pixi_remote_config.toml):")
        print(json.dumps(cfg, indent=2))
        return 0

    # NEW: Build the package, show its size breakdown, and exit
    if args.show_package_size:
        deployment_config = cfg.get("deployment", {})
        offline_mode = args.offline_mode or deployment_config.get("offline_mode", False)

        print("📦 Building package to measure size...")
        pkg = _package_dir(offline_mode=offline_mode, auto_approve=auto_approve)
        try:
            total_size = os.path.getsize(pkg)
            print(f"\n📦 Package Size Breakdown:")
            print(f"  Mode: {'offline (dependencies included)' if offline_mode else 'online'}")
            print(f"  Package file size: {total_size / (1024 * 1024):.2f} MB")

            entry_sizes = {}
            if pkg.endswith(".lz4"):
                import lz4.frame
                src = lz4.frame.open(pkg, "rb")
            else:
                src = open(pkg, "rb")
            with src, tarfile.open(fileobj=src, mode="r|") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    top = member.name.split("/", 1)[0]
                    entry_sizes[top] = entry_sizes.get(top, 0) + member.size

            uncompressed = sum(entry_sizes.values())
            print(f"  Uncompressed size: {uncompressed / (1024 * 1024):.2f} MB")
            print(f"  Top-level entries:")
            for name, size in sorted(entry_sizes.items(), key=lambda kv: kv[1], reverse=True):
                print(f"    {name}: {size / (1024 * 1024):.2f} MB")
        finally:
            os.unlink(pkg)
        return 0

    # NEW: Show MCP server configuration
    if args.show_mcp:
        print("🔧 MCP Server Configuration:")
        print("=" * 60)
        
        mcp_config = cfg.get("mcp", {})
        
        # Built-in filesystem server
        filesystem_config = mcp_config.get("filesystem", {})
        if filesystem_config.get("enabled", False):
            root_path = filesystem_config.get("root_path", ".")
            print(f"Built-in Filesystem Server: ✅ Enabled")
            print(f"  Root Path: {root_path}")
        else:
            print(f"Built-in Filesystem Server: ❌ Disabled")
        
        print()
        
        # External servers
        external_config = mcp_config.get("external_servers", {})
        if external_config.get("enabled", False):
            servers = external_config.get("server", [])
            if isinstance(servers, dict):
                servers = [servers]
            
            print(f"External MCP Servers: ✅ Enabled ({len(servers)} configured)")
            print(f"Fallback to Built-in: {'✅' if external_config.get('fallback_to_builtin', True) else '❌'}")
            print()
            
            for i, server in enumerate(servers, 1):
                name = server.get("name", f"server-{i}")
                url = server.get("url", "")
                server_type = server.get("type", "generic")
                actions = server.get("actions", [])
                auth_type = server.get("auth_type", "none")
                priority = server.get("priority", 10)
                
                print(f"{i}. {name}")
                print(f"   URL: {url}")
                print(f"   Type: {server_type}")
                print(f"   Auth: {auth_type}")
                print(f"   Priority: {priority}")
                print(f"   Actions: {len(actions)}")
                
                if actions:
                    action_preview = actions[:5]  # Show first 5 actions
                    if len(actions) > 5:
                        action_preview.append(f"... and {len(actions) - 5} more")
                    print(f"     {', '.join(action_preview)}")
                
                if i < len(servers):
                    print()
            
            print("\n💡 Test Commands:")
            print("  # Test external MCP server routing:")
            print("  python enhanced_client.py --task test_mcp --with-mcp --with-external-mcp")
            
        else:
            print(f"External MCP Servers: ❌ Disabled")
            print("  Enable with: [config.mcp.external_servers] enabled = true")
        
        print("\n🔧 Configuration Example:")
        print("""
[config.mcp.external_servers]
enabled = true
fallback_to_builtin = true

[[config.mcp.external_servers.server]]
name = "mindsdb"
url = "http://localhost:47334"
type = "mindsdb"
auth_type = "none"
priority = 1
actions = ["mindsdb_query", "mindsdb_predict", "mindsdb_list_models"]

[[config.mcp.external_servers.server]]
name = "postgres"
url = "http://localhost:8080"
type = "generic"
auth_type = "bearer"
auth_token = "${env:POSTGRES_TOKEN}"
priority = 2
actions = ["sql_*", "schema_*"]
        """)
        
        return 0
    
    if args.show_proxy:
        print("🌐 HTTP Proxy URL Mappings:")
        print("=" * 60)
        
        proxy_config = cfg.get("proxy", {})
        if not proxy_config.get("enabled", False):
            print("  HTTP Proxy is disabled")
            print("  Enable with: [config.proxy] enabled = true")
            return 0
        
        remote_port = proxy_config.get("remote_port", 8080)
        mappings = proxy_config.get("mapping", [])
        
        if isinstance(mappings, dict):
            mappings = [mappings]
        elif not isinstance(mappings, list):
            mappings = []
        
        if not mappings:
            print("  No proxy mappings configured")
            print("  Add mappings with: [[config.proxy.mapping]]")
            return 0
        
        print(f"Remote Proxy Port: {remote_port}")
        
        # Show test server configuration
        enable_test_server_config = proxy_config.get("enable_local_test_server")
        if enable_test_server_config is not None:
            test_server_status = "enabled" if enable_test_server_config else "disabled"
            print(f"Local Test Server (config): {test_server_status}")
        else:
            print(f"Local Test Server (config): default (enabled)")
        
        print()
        
        for i, mapping in enumerate(mappings, 1):
            name = mapping.get("name", f"mapping-{i}")
            remote_path = mapping.get("remote_path", "/*")
            local_url = mapping.get("local_url", "http://localhost")
            
            print(f"{i}. {name}")
            print(f"   Remote: http://localhost:{remote_port}{remote_path}")
            print(f"   Local:  {local_url}")
            
            # Show header transformations
            add_headers = mapping.get("add_headers", {})
            remove_headers = mapping.get("remove_headers", [])
            replace_headers = mapping.get("replace_headers", {})
            
            if add_headers:
                print(f"   Add Headers: {list(add_headers.keys())}")
            if remove_headers:
                print(f"   Remove Headers: {remove_headers}")
            if replace_headers:
                print(f"   Replace Headers: {list(replace_headers.keys())}")
            
            # Show example test commands
            print(f"   Test Commands:")
            if remote_path.endswith("/*"):
                base_path = remote_path[:-2]
                print(f"     curl http://localhost:{remote_port}{base_path}/health")
                print(f"     curl http://localhost:{remote_port}{base_path}/status")
                if base_path == "/api":
                    print(f"     curl -X POST -H 'Content-Type: application/json' -d '{{\"test\":\"data\"}}' http://localhost:{remote_port}{base_path}/echo")
            elif remote_path == "/*":
                print(f"     curl http://localhost:{remote_port}/")
                print(f"     curl http://localhost:{remote_port}/health")
            else:
                print(f"     curl http://localhost:{remote_port}{remote_path}")
            
            if i < len(mappings):
                print()
        
        print("\n💡 Quick Test:")
        print(f"   1. Start local server: python local_test_server.py")  
        print(f"   2. Deploy task: python enhanced_client.py --task your_task --with-proxy")
        print(f"   3. Test from remote: curl http://localhost:{remote_port}/api/health")
        
        print("\n🔧 Test Server Control:")
        print(f"   Config file:  enable_local_test_server = true/false")
        print(f"   CLI enable:   --local-test-server (overrides config)")
        print(f"   CLI disable:  --no-local-test-server (overrides config)")
        
        return 0
    
    # NEW: Show environment variables that would be injected
    if args.show_env:
        print("🌍 Environment Variables Preview:")
        print("=" * 50)
        
        # Create temporary environment handler for preview
        client_info = {"ip": "preview", "proxy_port": 8080, "hostname": "preview"}
        temp_handler = EnvironmentHandler(cfg, "preview-task", client_info)
        
        if temp_handler.is_enabled():
            env_vars = temp_handler.get_environment_variables(args.env_profile)
            if env_vars:
                for key, value in sorted(env_vars.items()):
                    # Hide sensitive values in preview
                    if any(secret in key.lower() for secret in ['password', 'secret', 'key', 'token']):
                        display_value = "***"
                    else:
                        display_value = value
                    print(f"  {key}={display_value}")
                print(f"\nTotal: {len(env_vars)} variables")
                if args.env_profile:
                    print(f"Profile: {args.env_profile}")
            else:
                print("  No environment variables configured")
        else:
            print("  Environment injection is disabled")
            print("  Enable with: [config.environment] enabled = true")
        
        return 0
    
    # Globals already set via precedence resolution above
    
    # ➊ Check for existing AES key file FIRST (highest priority)
    if args.aes_key and Path(args.aes_key).exists():
        try:
            raw = Path(args.aes_key).read_bytes()
            try:
                cand = base64.b64decode(raw.strip(), validate=True)
                aes_key = cand if len(cand) == 32 else raw
            except Exception:
                aes_key = raw
            if len(aes_key) != 32:
                raise ValueError(f"Invalid AES key length: {len(aes_key)} (expected 32)")
            
            key_source = "auto-detected" if args.aes_key == "aes.key" and Path("aes.key").exists() else "specified"
            print(f"✅ AES key loaded from {key_source} file: {args.aes_key}")
            print(f"🔐 Encryption enabled with {len(aes_key)*8}-bit AES key")
            
            # Skip handshake if we have a valid key file
            if args.handshake_secret:
                print(f"💡 Skipping handshake - using existing key file instead")
                
        except Exception as e:
            print(f"❌ Failed to load AES key from {args.aes_key}: {e}")
            print("⚠️ Falling back to handshake...")
            aes_key = None
    
    # ➋ Only do handshake if no valid AES key file exists
    if aes_key is None and args.handshake_secret:
        aes_key, _ = perform_handshake(args.handshake_secret, rotate=False)
        print(f"✅ Handshake completed with secret from {'CLI' if args.handshake_secret != cfg.get('handshake-secret') and args.handshake_secret != cfg.get('handshake_secret') else 'config'}")
    elif aes_key is None and args.rotate_secret:
        aes_key, _ = perform_handshake(args.rotate_secret, rotate=True)
    # ➌ Final fallback for other key files without handshake
    elif aes_key is None and args.aes_key:
        print(f"⚠️ AES key file {args.aes_key} not found - no encryption available")

    # Sync resolved key into shared state for the transport + signal handler
    transport.aes_key = aes_key

    # ─── run / attach as normal (matching original) ─────────────────────────────────────────
    if args.attach_history:
        _attach_history(args.attach_history, _auth_headers)
    elif args.attach:
        _attach(args.attach, _auth_headers)
    else:
        # Enhanced: Check for MCP + Proxy + Offline configuration
        features_enabled = {}
        
        # Check for offline mode from CLI or config
        deployment_config = cfg.get("deployment", {})
        
        offline_mode = (args.offline_mode or 
                       deployment_config.get("offline_mode", False))
        
        validate_deps = (args.validate_dependencies or 
                        deployment_config.get("validate_dependencies", False))
        
        if offline_mode:
            print("🔒 Offline deployment mode enabled")
            if validate_deps:
                print("🔍 Dependency validation enabled")
        
        # MCP Configuration (enhanced with external server support)
        if (args.with_mcp or args.mcp_filesystem or args.with_external_mcp or 
            cfg.get("mcp", {}).get("filesystem", {}).get("enabled", False) or
            cfg.get("mcp", {}).get("external_servers", {}).get("enabled", False)):
            
            mcp_root = (args.mcp_root or 
                       cfg.get("mcp", {}).get("filesystem", {}).get("root_path") or 
                       cfg.get("mcp-root") or 
                       ".")
            
            # Create MCP configuration with both built-in and external settings
            mcp_config = {
                "filesystem": {
                    "enabled": args.mcp_filesystem or cfg.get("mcp", {}).get("filesystem", {}).get("enabled", False),
                    "root_path": mcp_root
                }
            }
            
            # Include full external servers configuration if available
            if args.with_external_mcp or cfg.get("mcp", {}).get("external_servers", {}).get("enabled", False):
                external_servers_config = cfg.get("mcp", {}).get("external_servers", {})
                mcp_config["external_servers"] = {
                    "enabled": True,
                    "fallback_to_builtin": external_servers_config.get("fallback_to_builtin", True),
                    **{k: v for k, v in external_servers_config.items() if k not in ["enabled", "fallback_to_builtin"]}
                }
            
            features_enabled["mcp"] = mcp_config
        
        # NEW: HTTP Proxy Configuration with enhanced test server control
        proxy_config = cfg.get("proxy", {})
        if args.with_proxy or proxy_config.get("enabled", False):
            proxy_port = args.proxy_port or proxy_config.get("remote_port", 8080)
            
            # Copy proxy config and override port if specified
            updated_proxy_config = dict(proxy_config)
            updated_proxy_config["enabled"] = True
            updated_proxy_config["remote_port"] = proxy_port
            
            # NEW: Enhanced test server control with proper precedence
            # Precedence: CLI flags > config file > default (True)
            if args.local_test_server:
                # CLI explicitly enables test server (highest priority)
                updated_proxy_config["enable_local_test_server"] = True
                print("🧪 Local test server: force enabled (CLI override)")
            elif args.no_local_test_server:
                # CLI explicitly disables test server (second highest priority)  
                updated_proxy_config["enable_local_test_server"] = False
                print("🧪 Local test server: force disabled (CLI override)")
            elif "enable_local_test_server" in proxy_config:
                # Use config file setting (third priority)
                config_setting = proxy_config["enable_local_test_server"]
                updated_proxy_config["enable_local_test_server"] = config_setting
                status = "enabled" if config_setting else "disabled"
                print(f"🧪 Local test server: {status} (from config file)")
            else:
                # Default to enabled (lowest priority)
                updated_proxy_config["enable_local_test_server"] = True
                print("🧪 Local test server: enabled (default)")
            
            features_enabled["proxy"] = updated_proxy_config
        
        # Determine automation configuration
        automation_cfg = cfg.get("mcp", {}).get("automation", {})
        
        # Determine mode from various sources
        mode = "timeout"  # default
        if args.auto_exit or automation_cfg.get("auto_exit"):
            mode = "auto_exit"
        elif args.keep_alive or automation_cfg.get("keep_alive"):
            mode = "keep_alive"
        elif automation_cfg.get("mode"):
            mode = automation_cfg["mode"]
        
        if features_enabled.get("mcp"):
            features_enabled["mcp"]["automation"] = {
                "mode": mode,
                "timeout_seconds": args.timeout or automation_cfg.get("timeout_seconds", 30),
                "max_wait_seconds": automation_cfg.get("max_wait_seconds", 120)
            }
        
        # Show resolved configuration
        print("🔧 Resolved Configuration:")
        print(f"  Server: {server_url} (from {server_source})")
        print(f"  Task: {args.task}")
        print(f"  Handshake Secret: {'✅ Configured' if args.handshake_secret else '❌ None'}")
        
        # Enhanced AES key file detection
        aes_status = "❌ None"
        if args.aes_key:
            if Path(args.aes_key).exists():
                aes_status = f"✅ Found ({args.aes_key})"
            else:
                aes_status = f"❌ Missing ({args.aes_key})"
        print(f"  AES Key File: {aes_status}")
        
        print(f"  Auth: {'✅ Configured' if _auth_headers else '❌ None'}")
        print(f"  MCP: {'✅ Enabled' if features_enabled.get('mcp') else '❌ Disabled'}")
        print(f"  HTTP Proxy: {'✅ Enabled' if features_enabled.get('proxy') else '❌ Disabled'}")
        
        # NEW: Environment injection status
        env_config = cfg.get("environment", {})
        env_enabled = env_config.get("enabled", False)
        env_status = "✅ Enabled" if env_enabled else "❌ Disabled"
        print(f"  Environment Injection: {env_status}")
        
        # NEW: Offline deployment status
        offline_status = "✅ Enabled" if offline_mode else "❌ Disabled (online mode)"
        print(f"  Offline Deployment: {offline_status}")
        
        # NEW: Auto-approve status
        auto_approve_status = "✅ Enabled" if auto_approve else "❌ Disabled"
        print(f"  Auto-approve: {auto_approve_status}")
        
        if features_enabled.get("mcp"):
            mcp_cfg = features_enabled["mcp"]
            
            # Built-in filesystem server info
            filesystem_enabled = mcp_cfg.get("filesystem", {}).get("enabled", False)
            if filesystem_enabled:
                filesystem_root = mcp_cfg.get("filesystem", {}).get("root_path", "N/A")
                print(f"    Built-in Filesystem: ✅ Enabled (root: {filesystem_root})")
            else:
                print(f"    Built-in Filesystem: ❌ Disabled")
            
            # External servers info
            external_enabled = mcp_cfg.get("external_servers", {}).get("enabled", False)
            if external_enabled:
                # Get the actual server count from the original configuration
                external_servers_config = cfg.get("mcp", {}).get("external_servers", {})
                servers = external_servers_config.get("server", [])
                if isinstance(servers, dict):
                    servers = [servers]
                elif not isinstance(servers, list):
                    servers = []
                
                fallback_enabled = mcp_cfg.get("external_servers", {}).get("fallback_to_builtin", True)
                print(f"    External Servers: ✅ Enabled ({len(servers)} configured)")
                print(f"    Fallback to Built-in: {'✅' if fallback_enabled else '❌'}")
                
                if servers:
                    print(f"\n🌐 External MCP Servers:")
                    print("=" * 50)
                    for i, server in enumerate(servers, 1):
                        name = server.get("name", f"server-{i}")
                        url = server.get("url", "")
                        server_type = server.get("type", "generic")
                        actions = server.get("actions", [])
                        priority = server.get("priority", 10)
                        
                        print(f"  {i}. {name} (priority: {priority})")
                        print(f"     URL: {url}")
                        print(f"     Type: {server_type}")
                        print(f"     Actions: {len(actions)}")
                        
                        if actions:
                            action_preview = actions[:3]  # Show first 3 actions
                            if len(actions) > 3:
                                action_preview.append(f"... +{len(actions) - 3} more")
                            print(f"       {', '.join(action_preview)}")
                        
                        if i < len(servers):
                            print()
                    print("=" * 50)
                else:
                    print(f"    ⚠️ External servers enabled but no servers configured")
            else:
                print(f"    External Servers: ❌ Disabled")
            
            # Automation settings
            automation = mcp_cfg.get("automation", {})
            print(f"    Mode: {automation.get('mode', 'timeout')}")
            print(f"    Timeout: {automation.get('timeout_seconds', 30)}s")
            print(f"    Max Wait: {automation.get('max_wait_seconds', 120)}s")
        
        # NEW: Detailed HTTP Proxy URL Mappings with test server status
        if features_enabled.get("proxy"):
            proxy_cfg = features_enabled["proxy"]
            remote_port = proxy_cfg.get('remote_port', 8080)
            test_server_enabled = proxy_cfg.get('enable_local_test_server', True)
            
            print(f"    Remote Port: {remote_port}")
            print(f"    Local Test Server: {'✅ Enabled' if test_server_enabled else '❌ Disabled'}")
            
            mappings = proxy_cfg.get("mapping", [])
            if isinstance(mappings, dict):
                mappings = [mappings]
            elif not isinstance(mappings, list):
                mappings = []
            
            print(f"    URL Mappings: {len(mappings)}")
            
            if mappings:
                print("\n📍 HTTP Proxy URL Mappings:")
                print("=" * 70)
                for i, mapping in enumerate(mappings, 1):
                    name = mapping.get("name", f"mapping-{i}")
                    remote_path = mapping.get("remote_path", "/*")
                    local_url = mapping.get("local_url", "http://localhost")
                    
                    print(f"  {i}. {name}")
                    print(f"     Remote: http://localhost:{remote_port}{remote_path}")
                    print(f"     Local:  {local_url}")
                    
                    # Show header transformations
                    add_headers = mapping.get("add_headers", {})
                    remove_headers = mapping.get("remove_headers", [])
                    replace_headers = mapping.get("replace_headers", {})
                    
                    transformations = []
                    if add_headers:
                        transformations.append(f"Add {len(add_headers)} headers")
                    if remove_headers:
                        transformations.append(f"Remove {len(remove_headers)} headers")
                    if replace_headers:
                        transformations.append(f"Replace {len(replace_headers)} headers")
                    
                    if transformations:
                        print(f"     Transform: {', '.join(transformations)}")
                    
                    # Show example URLs for testing
                    if remote_path.endswith("/*"):
                        base_path = remote_path[:-2]
                        print(f"     Test URLs:")
                        print(f"       curl http://localhost:{remote_port}{base_path}/health")
                        print(f"       curl http://localhost:{remote_port}{base_path}/status")
                        if base_path == "/api":
                            print(f"       curl http://localhost:{remote_port}{base_path}/users")
                            print(f"       curl -X POST http://localhost:{remote_port}{base_path}/data")
                    elif remote_path == "/*":
                        print(f"     Test URLs:")
                        print(f"       curl http://localhost:{remote_port}/")
                        print(f"       curl http://localhost:{remote_port}/health")
                    else:
                        print(f"     Test URL:")
                        print(f"       curl http://localhost:{remote_port}{remote_path}")
                    
                    if i < len(mappings):
                        print()
                
                print("=" * 70)
                print(f"💡 All requests to http://localhost:{remote_port} will be proxied through encrypted tunnel")
                print(f"🔒 Headers will be transformed and local tokens injected automatically")
                
                if test_server_enabled:
                    print(f"🧪 Local test server will be available for immediate testing")
                else:
                    print(f"⏭️  Local test server disabled - testing only available from remote tasks")
                print()
        
        # NEW: Environment details
        if env_enabled:
            env_vars_count = len(env_config.get("variables", {}))
            print(f"    Base Variables: {env_vars_count}")
            if args.env_profile:
                profile_vars = len(env_config.get(args.env_profile, {}))
                print(f"    Profile ({args.env_profile}): {profile_vars} variables")
            conditional_config = env_config.get("conditional", {})
            conditional_count = len(conditional_config)
            if conditional_count > 0:
                print(f"    Conditional Blocks: {conditional_count}")
        
        # NEW: Offline deployment details
        if offline_mode:
            print(f"    Dependencies: Will be packaged locally")
            print(f"    Server Requirements: No internet connection needed")
            if validate_deps:
                print(f"    Validation: Pre-flight checks enabled")
        else:
            print(f"    Dependencies: Will be downloaded on server")
            print(f"    Server Requirements: Internet connection required")
        
        print()
        
        # Enhanced encryption validation with corrected priority
        encryption_configured = bool((args.aes_key and Path(args.aes_key).exists()) or args.handshake_secret)
        if not encryption_configured:
            print("⚠️ Warning: No encryption method configured or available.")
            print("💡 Note: Communication will be unencrypted.")
        else:
            if args.aes_key and Path(args.aes_key).exists():
                source = "auto-detected" if args.aes_key == "aes.key" else "specified"
                print(f"🔐 Encryption method: AES Key File ({source}) - skipping handshake")
            elif args.handshake_secret:
                source = "config" if (args.handshake_secret == cfg.get("handshake-secret") or args.handshake_secret == cfg.get("handshake_secret")) else "CLI"
                print(f"🔐 Encryption method: Handshake (from {source})")
        print()
        
        # Update configuration with features
        enhanced_config = dict(cfg)
        if features_enabled.get("mcp"):
            enhanced_config["mcp"] = features_enabled["mcp"]
        if features_enabled.get("proxy"):
            enhanced_config["proxy"] = features_enabled["proxy"]
        
        if features_enabled or offline_mode:
            # Enhanced deployment with MCP + Proxy + Offline and automation support
            print("🚀 One-Step Deployment with Enhanced Features")
            
            feature_list = []
            if features_enabled.get("mcp"):
                feature_list.append("MCP Back-Channel")
            if features_enabled.get("proxy"):
                feature_list.append("HTTP Proxy")
            if env_enabled:
                feature_list.append("Environment Injection")
            if offline_mode:
                feature_list.append("Offline Dependencies")
            if auto_approve:
                feature_list.append("Auto-Approve")
            
            if feature_list:
                print(f"✨ Features: {', '.join(feature_list)}")
            
            # SECURITY CHECK: Warn if features are enabled without encryption
            encryption_available = bool(args.handshake_secret or (args.aes_key and Path(args.aes_key).exists()))
            if (features_enabled or offline_mode) and not encryption_available:
                print("🚨 SECURITY WARNING: Enhanced features enabled WITHOUT encryption!")
                print("💡 This means all data will be transmitted in PLAIN TEXT")
                print("🔐 Strongly recommend enabling encryption via handshake-secret or aes-key")
                
                if auto_approve:
                    print("⚠️ Auto-approve enabled - continuing without encryption (SECURITY RISK)")
                    print("🔐 Consider adding encryption for production use")
                else:
                    # Ask for confirmation in interactive mode
                    if sys.stdin.isatty():
                        try:
                            confirm = input("Continue without encryption? (y/N): ").strip().lower()
                            if confirm not in ['y', 'yes']:
                                print("❌ Deployment cancelled for security reasons")
                                return 1
                        except KeyboardInterrupt:
                            print("\n❌ Deployment cancelled")
                            return 1
                    else:
                        print("⚠️ Non-interactive mode: continuing without encryption")
            
            client = OneStepPixiClient(server_url, args.aes_key, _auth_headers, args.handshake_secret, args.env_profile, auto_approve)
            
            try:
                task_id = client.deploy_task_with_full_features(args.task, enhanced_config, offline_mode)
                
                automation = enhanced_config.get("mcp", {}).get("automation", {})
                mode = automation.get("mode", "timeout")  # Default to timeout mode
                
                if mode == "keep_alive":
                    # Traditional keep-alive mode - run until Ctrl+C
                    client.keep_alive()
                    
                elif mode == "auto_exit":
                    # FIXED AUTO-EXIT MODE: wait for operations then exit
                    print(f"\n✅ Deployment complete! Task ID: {task_id}")
                    print("🤖 Auto-exit mode: Waiting for operations to complete...")
                    
                    # Wait for the listener to signal completion
                    max_wait = automation.get("max_wait_seconds", 120)
                    start_time = time.time()
                    
                    while not hasattr(client, '_should_auto_exit') or not client._should_auto_exit:
                        if time.time() - start_time > max_wait:
                            print(f"⏰ Maximum wait time ({max_wait}s) exceeded - exiting")
                            break
                        time.sleep(0.1)
                    
                    print("🏁 Auto-exit complete")
                    
                else:  # mode == "timeout" or default
                    # Timeout mode: wait specified time then exit
                    timeout = automation.get("timeout_seconds", 30)
                    print(f"\n✅ Deployment complete! Task ID: {task_id}")
                    print(f"⏰ Keeping enhanced back-channel alive for {timeout} seconds...")
                    
                    try:
                        time.sleep(timeout)
                        print(f"⏰ Timeout reached, exiting gracefully")
                    except KeyboardInterrupt:
                        print("\n🛑 Interrupted by user")
                
            except Exception as e:
                print(f"❌ Deployment failed: {e}")
                return 1
        else:
            # ENHANCED: Standard deployment with environment injection, offline support, and auto-approve
            client = OneStepPixiClient(server_url, args.aes_key, _auth_headers, args.handshake_secret, args.env_profile, auto_approve)
            task_id = client.deploy_task_standard(args.task, enhanced_config, offline_mode)
            deployment_type = "offline" if offline_mode else "online"
            auto_approve_note = " (auto-approved)" if auto_approve else ""
            print(f"\n✅ Standard deployment complete! Task ID: {task_id}")
            print(f"💻 Task running without enhanced features ({deployment_type} mode{auto_approve_note})")


if __name__ == "__main__":
    main()
