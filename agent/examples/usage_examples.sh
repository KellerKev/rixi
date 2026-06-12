# usage_examples.sh - Usage examples for the clean architecture

#!/bin/bash

echo "🎯 Clean MCP Architecture Usage Examples"
echo "========================================"

# Example 1: Original agent (no changes)
echo -e "\n📝 Example 1: Original Agent (Backwards Compatible)"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --original-agent haiku --topic 'mountain stream'"

# Example 2: Simple MCP mode
echo -e "\n🔧 Example 2: Simple MCP Mode"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --haiku-mode simple --topic 'ocean waves'"

# Example 3: Research mode
echo -e "\n🔍 Example 3: Research-Enhanced Mode"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --haiku-mode research --topic 'quantum computing'"

# Example 4: Configuration-driven
echo -e "\n⚙️  Example 4: Configuration-Driven"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --config haiku_agent_config.yaml --topic 'artificial intelligence' --workflow research_haiku"

# Example 5: Custom configuration
echo -e "\n🎨 Example 5: Custom Configuration"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --config custom_config.yaml --context '{\"content_type\": \"paragraph\", \"style\": \"poetic\"}'"

# Example 6: Generic workflow
echo -e "\n🔄 Example 6: Generic Workflow"
echo "python start_agent_configurable.py --task-id <TASK_ID> --aes-key aes.key --config agent_config.yaml --workflow analysis_workflow --topic 'renewable energy'"

echo -e "\n📁 File Structure:"
echo "├── mcp_manager.py           # Pure MCP management"
echo "├── mcp_agent.py             # Generic configurable agent"
echo "├── haiku_agent_mcp.py       # Haiku-specific agent"
echo "├── start_agent_configurable.py  # Generic runner"
echo "├── agent_config.yaml        # Main configuration"
echo "├── haiku_agent_config.yaml  # Haiku-specific config"
echo "└── your_existing_files.py   # Unchanged!"

echo -e "\n✅ Key Benefits:"
echo "• Clean separation of concerns"
echo "• Configuration-driven behavior"
echo "• Backwards compatible"
echo "• No hardcoded content assumptions"
echo "• Extensible for any content type"
echo "• Easy to add new orchestrators (AWS, Google, etc.)"
