# remote_channel.py

import time
import json
import requests
from aesgcm import aes_encrypt, aes_decrypt

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

        return aes_encrypt(self.aes_key, json_str)

    def _decrypt(self, data):
        plaintext = aes_decrypt(self.aes_key, data)
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
