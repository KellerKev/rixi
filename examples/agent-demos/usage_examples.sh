#!/bin/bash
# usage_examples.sh - Usage examples for the agent framework
#
# The agent entry point and modules live in ../../agent (run these from there).
# The default config template is agent/agent_config.example.yaml. The fully-populated
# demo configs (agent_config.yaml, haiku_config.yaml) live in THIS folder; pass them
# by path, e.g. --config ../examples/agent-demos/agent_config.yaml.

DEMO_DIR="../examples/agent-demos"

echo "🎯 Agent Framework Usage Examples"
echo "========================================"

# Example 1: Simple workflow using the default template config
echo -e "\n📝 Example 1: Default Template Config"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --workflow simple_generation --topic 'ocean waves'"

# Example 2: Full demo config (multiple workflows)
echo -e "\n🔧 Example 2: Full Demo Config"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config $DEMO_DIR/agent_config.yaml --workflow simple_generation --topic 'mountain stream'"

# Example 3: Research-enhanced workflow
echo -e "\n🔍 Example 3: Research-Enhanced Mode"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config $DEMO_DIR/agent_config.yaml --workflow research_workflow --topic 'quantum computing'"

# Example 4: Haiku configuration
echo -e "\n⚙️  Example 4: Haiku Configuration"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config $DEMO_DIR/haiku_config.yaml --topic 'artificial intelligence' --workflow research_haiku"

# Example 5: Generic analysis workflow
echo -e "\n🔄 Example 5: Generic Workflow"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config $DEMO_DIR/agent_config.yaml --workflow analysis_workflow --topic 'renewable energy'"

echo -e "\n📁 Layout:"
echo "../../agent/start_agent.py              # Single entry point / runner"
echo "../../agent/agent_config.example.yaml   # Config template (start_agent default)"
echo "./agent_config.yaml                     # Full demo config (this folder)"
echo "./haiku_config.yaml                     # Haiku demo config (this folder)"

echo -e "\n✅ Key Benefits:"
echo "• Clean separation of concerns"
echo "• Configuration-driven behavior"
echo "• No hardcoded content assumptions"
echo "• Extensible for any content type"
echo "• Easy to add new orchestrators (AWS, Google, etc.)"
