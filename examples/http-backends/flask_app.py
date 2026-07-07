#!/usr/bin/env python3
"""
Test Flask Service - Comprehensive logging test
This service will output various log messages at different stages to test log capture
"""
import sys
import time
import json
from flask import Flask, request, jsonify

def main():
    print("🔍 STEP 1: Starting main function...")
    sys.stdout.flush()
    
    time.sleep(0.5)
    print("🎯 STEP 2: Starting Flask app in Pixi task...")
    sys.stdout.flush()
    
    print("🔧 STEP 3: Initializing Flask application...")
    app = Flask(__name__)
    sys.stdout.flush()
    
    print("📝 STEP 4: Setting up Flask routes...")
    
    @app.route('/')
    def hello():
        return jsonify({
            "message": "Hello from Flask in Pixi!",
            "status": "running",
            "service": "test-flask-service"
        })
    
    @app.route('/test')
    def test():
        return jsonify({"test": "success", "timestamp": time.time()})
    
    @app.route('/health')
    def health():
        return jsonify({"health": "ok"})
    
    # HTTP request handler for testing our proxy functionality
    @app.route('/echo', methods=['POST'])
    def echo():
        data = request.get_json() or {}
        return jsonify({
            "echo": data,
            "headers": dict(request.headers),
            "method": request.method
        })
    
    sys.stdout.flush()
    
    print("🌐 STEP 5: Flask routes configured successfully")
    print("🚀 STEP 6: Starting Flask development server...")
    print("📡 STEP 7: Server will run on http://127.0.0.1:5000")
    print("🔄 STEP 8: Server is starting now...")
    sys.stdout.flush()
    
    # Add some error output to stderr to test that too
    print("🧪 Testing stderr output - this should appear in logs", file=sys.stderr)
    sys.stderr.flush()
    
    # Start Flask app
    app.run(
        host='127.0.0.1', 
        port=5000, 
        debug=True,  # This should produce more startup logs
        use_reloader=False  # Disable reloader to avoid process complications
    )

if __name__ == "__main__":
    print("🌟 STARTUP: Python script started")
    sys.stdout.flush()
    main()
