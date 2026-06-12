# ai_agent_framework.py

import uuid
import traceback
import time
import json
import os
import requests
import base64
import argparse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class RemoteChannel:
    """Handles communication with a remote task via the Pixi Runner channel."""
    def __init__(self, server_url, task_id, aes_key=None, auth_headers=None):
        self.server_url = server_url
        self.task_id = task_id
        self.aes_key = aes_key
        self.auth_headers = auth_headers or {}
        self.NONCE_LEN = 12

    def send(self, data):
        """Send data to the remote task."""
        headers = self.auth_headers.copy()
        if self.aes_key:
            headers["Content-Type"] = "application/octet-stream"
            payload = self._encrypt(data)
        else:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(data).encode('utf-8') if isinstance(data, dict) else data.encode('utf-8')

        response = requests.post(
            f"{self.server_url}/task/{self.task_id}/input",
            data=payload,
            headers=headers
        )
        return self._handle_response(response)

    def receive(self, callback=None, timeout=30):
        """Receive data from the remote task."""
        headers = self.auth_headers.copy()
        response = requests.get(
            f"{self.server_url}/task/{self.task_id}/stream",
            headers=headers,
            stream=True,
            timeout=timeout
        )
        return self._process_stream(response, callback)

    def query_task_status(self):
        """Query the status of the remote task directly."""
        headers = self.auth_headers.copy()
        response = requests.get(
            f"{self.server_url}/task/{self.task_id}",
            headers=headers
        )
        return self._handle_response(response)

    def _encrypt(self, data):
        # Convert dict to JSON string if needed
        if isinstance(data, dict):
            json_str = json.dumps(data)
        else:
            json_str = data

        nonce = os.urandom(self.NONCE_LEN)
        return nonce + AESGCM(self.aes_key).encrypt(nonce, json_str.encode('utf-8'), None)

    def _decrypt(self, data):
        plaintext = AESGCM(self.aes_key).decrypt(data[:self.NONCE_LEN], data[self.NONCE_LEN:], None)
        return plaintext.decode('utf-8')


    def _handle_response(self, response):
        if response.status_code != 200:
            raise Exception(f"Error: {response.status_code} - {response.text}")
        return response.json()

    def _process_stream(self, response, callback=None):
        """Process length-prefixed encrypted stream from server."""
        start_time = time.time()
        buf = b""
        need = None

        for chunk in response.iter_content(chunk_size=4096):
            if self._check_timeout(start_time, time.time(), 30):
                print("Receive timed out after 30 seconds")
                break
                
            buf += chunk
            
            while True:
                # Read 4-byte length prefix
                if need is None and len(buf) >= 4:
                    need = int.from_bytes(buf[:4], "big")
                    buf = buf[4:]
                
                # Check if we have enough data for current frame
                if need is None or len(buf) < need:
                    break

                # Extract frame and decrypt
                enc, buf = buf[:need], buf[need:]
                need = None
                
                try:
                    # Decrypt the frame
                    data = self._decrypt(enc)
                    
                    # Process each line in the decrypted data
                    for line in data.split('\n'):
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                            if callback and callback(obj):
                                return  # Stop if callback returns True
                        except json.JSONDecodeError:
                            print(f"JSON decode error on line: {line[:100]}...")
                            continue

                except Exception as e:
                    print(f"Error processing frame: {e}")
                    continue

        return  # Clean exit 






    def _check_timeout(self, start_time, current_time, timeout):
        return (current_time - start_time) > timeout

class Agent:
    """Base class for AI agents that use remote inference."""
    def __init__(self, channel):
        self.channel = channel

    def send_to_remote(self, message):
        return self.channel.send(message)

    def receive_from_remote(self, callback=None):
        return self.channel.receive(callback)

    def perform_local_action(self, action_type, **params):
        raise NotImplementedError("Subclasses must implement perform_local_action")

class HaikuAgent(Agent):
    """Example agent that generates haikus and saves them locally."""
    def __init__(self, channel):
        super().__init__(channel)
        self.response_data = None
        self.request_id = None

    def generate_haiku(self, topic, output_file="haiku.txt"):
        print(f"Generating haiku about: {topic}")
        self.request_id = str(uuid.uuid4())
        self.current_prompt = f"Write a haiku about {topic}"

        self.send_to_remote({
            "command": "generate",
            "prompt": self.current_prompt,
            "request_id": self.request_id
        })

        def response_callback(data):
            if "output" in data:
                try:
                    output_obj = json.loads(data["output"])
                    if "request_id" in output_obj and output_obj["request_id"] == self.request_id:
                        print(f"Found matching response for request ID: {self.request_id}")
                        self.response_data = output_obj["response"]
                        return True
                    elif "request_id" in output_obj:
                        print(f"Skipping response for different request ID: {output_obj['request_id']}")

                except json.JSONDecodeError:
                    pass

            return False

        start_time = time.time()
        max_wait_time = 30
        self.receive_from_remote(response_callback)
        if not self.response_data and (time.time() - start_time < max_wait_time):
            print("No matching response found in stream, trying direct task status query...")
            while not self.response_data and (time.time() - start_time < max_wait_time):
                response = self.channel.query_task_status()
                if "recent_output" in response:
                    for output in response["recent_output"]:
                        if output["type"] == "output":
                            try:
                                output_obj = json.loads(output["content"])
                                if "request_id" in output_obj and output_obj["request_id"] == self.request_id:
                                    print(f"Found matching response via task status for request ID: {self.request_id}")
                                    self.response_data = output_obj["response"]
                                    break
                            except (json.JSONDecodeError, KeyError):
                                pass

                if self.response_data:
                    break
                time.sleep(1)

        if self.response_data:
            self.perform_local_action("write_file", filename=output_file, content=self.response_data)
            return self.response_data
        else:
            raise Exception("Failed to generate haiku - no matching response received")

    def perform_local_action(self, action_type, **params):
        if action_type == "write_file":
            filename = params.get("filename")
            content = params.get("content")
            with open(filename, "w") as f:
                f.write(content)
            print(f"Wrote content to {filename}")
        else:
            print(f"Unknown action type: {action_type}")

def read_pixi_config():
    if not os.path.exists("pixi_remote_config.toml"):
        return {}
    try:
        import tomli
        with open("pixi_remote_config.toml", "rb") as f:
            return tomli.load(f).get("config", {})
    except ImportError:
        print("tomli package not found. Using basic config.")
        return {}

def create_auth_headers(bearer=None, snowflake=None):
    headers = {}
    if snowflake:
        headers["Authorization"] = f'Snowflake Token="{snowflake}"'
    elif bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers

def main():
    parser = argparse.ArgumentParser(description="AI Agent Framework")
    parser.add_argument("--server", default="http://localhost:9000", help="Pixi Runner server URL")
    parser.add_argument("--task-id", required=True, help="Remote task ID to connect to")
    parser.add_argument("--aes-key", help="Path to AES key file (if encryption enabled)")
    parser.add_argument("--bearer-token", help="Bearer token for authentication")

    # Agent-specific options
    parser.add_argument("--agent", choices=["haiku"], default="haiku", help="Agent type")
    parser.add_argument("--topic", default="mountain stream", help="Topic for haiku generation")
    parser.add_argument("--output", default="haiku.txt", help="Output file")

    args = parser.parse_args()

    config = read_pixi_config()
    bearer_token = args.bearer_token or config.get("bearer_token") or config.get("bearer-token")
    snowflake_token = config.get("snowflake_token") or config.get("snowflake-token")
    server_url = args.server or config.get("server_url") or config.get("server-url", "http://localhost:9000")

    auth_headers = create_auth_headers(bearer_token, snowflake_token)

    aes_key = None
    if args.aes_key:
        with open(args.aes_key, "rb") as f:
            raw = f.read().strip()
            try:
                aes_key = base64.b64decode(raw)
                if len(aes_key) != 32:
                    print("Invalid AES key length")
                    return
                print("AES encryption enabled")
            except Exception as e:
                print(f"Error decoding AES key: {e}")
                return

    channel = RemoteChannel(server_url, args.task_id, aes_key, auth_headers)

    if args.agent == "haiku":
        agent = HaikuAgent(channel)
        result = agent.generate_haiku(args.topic, args.output)
        print("\nGenerated haiku:")
        print("-" * 40)
        print(result)
        print("-" * 40)

if __name__ == "__main__":
    main()
