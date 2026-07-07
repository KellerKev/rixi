"""Call a RIXI-hosted model through the OpenAI-compatible proxy.

The model backend (../../inference-server) runs as a keep-alive task on a rixi GPU server;
the proxy (../../proxy) exposes it at an OpenAI-compatible base URL. This script talks to
that URL with the stock OpenAI SDK — no RIXI-specific client needed on the calling side.

    RIXI_PROXY_URL=http://localhost:8002/v1 python call.py "haiku about the sea"
"""
import os
import sys

from openai import OpenAI

BASE_URL = os.environ.get("RIXI_PROXY_URL", "http://localhost:8002/v1")
MODEL = os.environ.get("MODEL", "gpt-3.5-turbo")  # proxy maps this to the deployed model


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Write a haiku about distributed systems."
    # The proxy doesn't check the key; any non-empty string satisfies the SDK.
    client = OpenAI(base_url=BASE_URL, api_key=os.environ.get("OPENAI_API_KEY", "rixi"))
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    print(resp.choices[0].message.content)


if __name__ == "__main__":
    main()
