#!/bin/bash
# usage_examples.sh - Usage examples for the agent framework
#
# These commands run the agent entry point, which lives in ../../agent.
# Run them from that directory (cd ../../agent) where the configs and modules live.

echo "🎯 Agent Framework Usage Examples"
echo "========================================"

# Example 1: Simple haiku workflow
echo -e "\n📝 Example 1: Simple Haiku Workflow"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config agent_config.yaml --workflow simple_generation --topic 'mountain stream'"

# Example 2: Simple workflow using the default config
echo -e "\n🔧 Example 2: Default Config (agent_config.yaml)"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --workflow simple_generation --topic 'ocean waves'"

# Example 3: Research-enhanced workflow
echo -e "\n🔍 Example 3: Research-Enhanced Mode"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config agent_config.yaml --workflow research_workflow --topic 'quantum computing'"

# Example 4: Haiku configuration
echo -e "\n⚙️  Example 4: Haiku Configuration"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config haiku_config.yaml --topic 'artificial intelligence' --workflow research_haiku"

# Example 5: Custom configuration
echo -e "\n🎨 Example 5: Custom Configuration"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config custom_config.yaml --context '{\"content_type\": \"paragraph\", \"style\": \"poetic\"}'"

# Example 6: Generic workflow
echo -e "\n🔄 Example 6: Generic Workflow"
echo "python start_agent.py --task-id <TASK_ID> --aes-key aes.key --config agent_config.yaml --workflow analysis_workflow --topic 'renewable energy'"

echo -e "\n📁 Agent file structure (in ../../agent):"
echo "├── mcp_manager.py           # Pure MCP management"
echo "├── mcp_agent.py             # Generic configurable agent"
echo "├── start_agent.py           # Single entry point / runner"
echo "├── agent_config.yaml        # Main configuration"
echo "└── haiku_config.yaml        # Haiku demo configuration"

echo -e "\n✅ Key Benefits:"
echo "• Clean separation of concerns"
echo "• Configuration-driven behavior"
echo "• Backwards compatible"
echo "• No hardcoded content assumptions"
echo "• Extensible for any content type"
echo "• Easy to add new orchestrators (AWS, Google, etc.)"
