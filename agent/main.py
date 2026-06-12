#!/usr/bin/env python3
"""
Generic HTTP Backend Examples
Shows how to create various backend services that work with the HTTP proxy
"""
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# ─────────────────────────── Example 1: Simple HTTP Server ────────────────

class SimpleHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = {"message": "Hello from Simple HTTP Server!", "timestamp": time.time()}
            self.wfile.write(json.dumps(response).encode())
        
        elif self.path.startswith('/api/data'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response = {"data": [1, 2, 3, 4, 5], "server": "simple_http"}
            self.wfile.write(json.dumps(response).encode())
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode())
        except:
            data = {"raw": post_data.decode()}
        
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        
        response = {
            "received": data,
            "path": self.path,
            "method": "POST",
            "server": "simple_http",
            "timestamp": time.time()
        }
        self.wfile.write(json.dumps(response).encode())

def run_simple_http_server():
    server = HTTPServer(('127.0.0.1', 8080), SimpleHTTPHandler)
    server.serve_forever()

# ─────────────────────────── Example 2: FastAPI Backend ───────────────────

FASTAPI_EXAMPLE = '''
from fastapi import FastAPI, Request
import json

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Hello from FastAPI!", "framework": "fastapi"}

@app.get("/api/users/{user_id}")
async def get_user(user_id: int):
    return {"user_id": user_id, "name": f"User {user_id}", "framework": "fastapi"}

@app.post("/api/process")
async def process_data(request: Request):
    data = await request.json()
    return {"processed": data, "status": "success", "framework": "fastapi"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
'''

# ─────────────────────────── Example 3: Generic Proxy Handler ─────────────

class GenericHTTPProxyHandler:
    """Generic handler that can proxy to any HTTP service"""
    
    def __init__(self, target_port=8080):
        self.target_port = target_port
        self.base_url = f"http://127.0.0.1:{target_port}"
    
    def handle_http_request(self, request_data):
        """Handle HTTP request and return response via stdout"""
        try:
            method = request_data.get("method", "GET")
            path = request_data.get("path", "/")
            query = request_data.get("query", "")
            headers = request_data.get("headers", {})
            body = request_data.get("body")
            
            # Build URL
            url = f"{self.base_url}/{path}"
            if query:
                url += f"?{query}"
            
            print(f"🔗 Proxying {method} {url}", file=sys.stderr)
            
            # Make request to target service
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=10)
            elif method == "POST":
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except:
                        pass
                resp = requests.post(url, json=body, headers=headers, timeout=10)
            elif method == "PUT":
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except:
                        pass
                resp = requests.put(url, json=body, headers=headers, timeout=10)
            elif method == "DELETE":
                resp = requests.delete(url, headers=headers, timeout=10)
            else:
                resp = requests.request(method, url, headers=headers, timeout=10)
            
            # Send response back via stdout
            response_data = {
                "type": "http_response",
                "request_id": request_data.get("request_id", "unknown"),
                "data": {
                    "status": resp.status_code,
                    "headers": dict(resp.headers),
                    "body": resp.text
                }
            }
            
            print(json.dumps(response_data))
            sys.stdout.flush()
            
        except Exception as e:
            # Send error response
            error_response = {
                "type": "http_response",
                "request_id": request_data.get("request_id", "unknown"),
                "data": {
                    "status": 500,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": str(e)})
                }
            }
            print(json.dumps(error_response))
            sys.stdout.flush()

# ─────────────────────────── Main Pixi Task Handler ───────────────────────

def main():
    """Main function for Pixi task that handles HTTP proxy requests"""
    print("🚀 Starting Generic HTTP Backend Service...", file=sys.stderr)
    
    # Start the actual HTTP service in a thread
    service_thread = threading.Thread(target=run_simple_http_server, daemon=True)
    service_thread.start()
    
    # Give service time to start
    time.sleep(1)
    
    print("✅ HTTP service ready on port 8080", file=sys.stderr)
    print("📡 Listening for proxy requests from stdin...", file=sys.stderr)
    
    # Create proxy handler
    proxy_handler = GenericHTTPProxyHandler(target_port=8080)
    
    # Handle proxy requests from stdin
    for line in sys.stdin:
        try:
            if not line.strip():
                continue
                
            data = json.loads(line.strip())
            
            if data.get("type") == "http_request":
                proxy_handler.handle_http_request(data)
            else:
                # Handle other types of requests
                print(f"📥 Received other request: {data.get('type', 'unknown')}", file=sys.stderr)
                
        except json.JSONDecodeError:
            print(f"❌ Invalid JSON: {line.strip()}", file=sys.stderr)
        except Exception as e:
            print(f"💥 Error handling request: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()

# ─────────────────────────── Usage Examples ───────────────────────────────

USAGE_EXAMPLES = '''
# ─────────────────────────── Usage Examples ───────────────────────────────

## 1. Package and Deploy
tar -czf http-service.tar.gz main.py pixi.toml
lz4 -c http-service.tar.gz > http-service.lz4

curl -X POST http://localhost:9000/upload \\
  -F "file=@http-service.lz4" \\
  -F "task_name=http-service" \\
  -F "keep_alive=true" \\
  -H "Authorization: Bearer your-jwt-token"

## 2. Test HTTP Proxy Routes

# Simple GET request
curl http://localhost:8002/proxy/

# GET with path
curl http://localhost:8002/proxy/api/data

# POST with JSON data
curl -X POST http://localhost:8002/proxy/api/process \\
  -H "Content-Type: application/json" \\
  -d '{"name": "test", "value": 123}'

# API-prefixed routes  
curl http://localhost:8002/api/proxy/users/456

# HTTP service routes
curl http://localhost:8002/http/api/data

## 3. Different Backend Types

### Flask Backend
curl http://localhost:8002/proxy/flask/users/123

### FastAPI Backend  
curl http://localhost:8002/proxy/api/users/456

### Generic HTTP Service
curl http://localhost:8002/proxy/api/data

### REST API
curl -X PUT http://localhost:8002/proxy/api/users/123 \\
  -H "Content-Type: application/json" \\
  -d '{"name": "Updated User"}'

### File Upload Simulation
curl -X POST http://localhost:8002/proxy/upload \\
  -H "Content-Type: application/json" \\
  -d '{"filename": "doc.pdf", "content": "base64data"}'

## 4. With AES Encryption (Transparent)
# Same commands work - encryption handled automatically
curl -X POST http://localhost:8002/proxy/api/secure \\
  -H "Content-Type: application/json" \\
  -d '{"secret": "encrypted_automatically"}'
'''
