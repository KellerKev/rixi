"""crewai_remote_bridge.py — run a CrewAI crew against a REMOTE RIXI inference backend.

Unlike the crewai-showcase (which drives local Ollama models), this bridges CrewAI's LLM
calls to a model served as a RIXI task: `RemoteInferenceLLM` forwards each generation over
the encrypted back-channel to the remote inference server and returns the completion, so a
whole CrewAI orchestration runs against your self-hosted remote model.

    # 1) deploy inference-server as a keep-alive task, note its task id + aes.key
    # 2) run a crew against it:
    python crewai_remote_bridge.py            # uses aes.key + a built-in test config
"""
import os
import sys

# Import the agent engine (ai_agent_framework) from the sibling agent/ package.
_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

import asyncio
import json
import uuid
from typing import Any, Dict, List

from crewai import Agent, Crew, Process, Task

from ai_agent_framework import RemoteChannel


class RemoteInferenceLLM:
    """Bridge CrewAI LLM calls to the remote inference system."""

    def __init__(self, channel: RemoteChannel):
        self.channel = channel
        self.model = "remote-inference-system"
        self.model_name = "remote-inference-system"

    def call(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """Route a CrewAI LLM call to remote inference and return the completion."""
        try:
            prompt = self._messages_to_prompt(messages)
            request_id = str(uuid.uuid4())
            self.channel.send({
                "command": "generate",
                "prompt": prompt,
                "request_id": request_id,
            })

            response_data = None

            def response_callback(data):
                nonlocal response_data
                if "output" in data:
                    try:
                        output_obj = json.loads(data["output"])
                        if output_obj.get("request_id") == request_id:
                            response_data = output_obj["response"]
                            return True
                    except json.JSONDecodeError:
                        pass
                return False

            self.channel.receive(response_callback)
            if response_data:
                return response_data
            return "I apologize, but I'm having trouble generating a response right now."
        except Exception as e:
            return f"Error: {str(e)}"

    def _messages_to_prompt(self, messages: List[Dict[str, str]]) -> str:
        prompt_parts = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if role == "system":
                prompt_parts.append(f"System: {content}")
            elif role == "user":
                prompt_parts.append(f"User: {content}")
            elif role == "assistant":
                prompt_parts.append(f"Assistant: {content}")
            else:
                prompt_parts.append(content)
        return "\n\n".join(prompt_parts)

    def generate(self, prompt: str, **kwargs) -> str:
        return self.call([{"role": "user", "content": prompt}], **kwargs)

    def __str__(self):
        return f"RemoteInferenceLLM(model={self.model})"

    def __repr__(self):
        return self.__str__()


class RemoteCrewAI:
    """CrewAI wired to the remote inference system."""

    def __init__(self, channel: RemoteChannel, config: Dict[str, Any]):
        self.channel = channel
        self.config = config
        self.remote_llm = RemoteInferenceLLM(channel)

    def create_agent(self, agent_config: Dict[str, Any]) -> Agent:
        return Agent(
            role=agent_config.get("role", "Assistant"),
            goal=agent_config.get("goal", "Complete assigned tasks"),
            backstory=agent_config.get("backstory", "I am an AI assistant"),
            llm=self.remote_llm,  # generation runs on the remote model
            tools=agent_config.get("tools", []),
            verbose=agent_config.get("verbose", True),
            allow_delegation=agent_config.get("allow_delegation", False),
        )

    def create_crew(self, agents: List[Agent], tasks: List[Task]) -> Crew:
        return Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=True)


async def run_crewai_with_remote_inference(config_path: str, server_url: str, task_id: str,
                                           aes_key: bytes = None):
    """Run a CrewAI crew (defined in a YAML config) against remote inference."""
    import yaml

    with open(config_path) as f:
        config = yaml.safe_load(f)

    from ai_agent_framework import create_auth_headers, read_pixi_config
    pixi_config = read_pixi_config()
    auth_headers = create_auth_headers(pixi_config.get("bearer_token"))

    channel = RemoteChannel(server_url, task_id, aes_key, auth_headers)
    remote_crew = RemoteCrewAI(channel, config)

    agents = [remote_crew.create_agent(a) for a in config.get("agents", [])]
    tasks = [
        Task(description=t["description"],
             agent=agents[t.get("agent_index", 0)],
             expected_output=t.get("expected_output", "Completed task"))
        for t in config.get("tasks", [])
    ]

    crew = remote_crew.create_crew(agents, tasks)
    print("Starting CrewAI against the remote inference system…")
    result = crew.kickoff()
    print("CrewAI execution completed using remote inference.")
    return result


def create_simple_test_config():
    return {
        "agents": [{
            "role": "Haiku Creator",
            "goal": "Create beautiful haiku about the given topic",
            "backstory": "I am a poet who specializes in traditional Japanese haiku",
            "verbose": True,
        }],
        "tasks": [{
            "description": "Create a haiku about quantum computing",
            "agent_index": 0,
            "expected_output": "A beautiful 5-7-5 syllable haiku about quantum computing",
        }],
    }


async def test_crewai_remote():
    import base64

    with open("aes.key", "rb") as f:
        aes_key = base64.b64decode(f.read().strip())

    config = create_simple_test_config()

    import yaml
    with open("temp_remote_test.yaml", "w") as f:
        yaml.dump(config, f)

    try:
        result = await run_crewai_with_remote_inference(
            "temp_remote_test.yaml",
            os.environ.get("RIXI_SERVER", "http://localhost:9000"),
            os.environ.get("RIXI_TASK_ID", ""),
            aes_key,
        )
        print(f"SUCCESS. CrewAI result: {result}")
        return True
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        if os.path.exists("temp_remote_test.yaml"):
            os.remove("temp_remote_test.yaml")


if __name__ == "__main__":
    asyncio.run(test_crewai_remote())
