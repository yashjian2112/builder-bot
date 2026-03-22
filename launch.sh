#!/bin/bash
# One-click launcher for Builder Bot
cd "$(dirname "$0")"

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "❌  ANTHROPIC_API_KEY not set."
  echo "    Add this to your ~/.zshrc or ~/.bash_profile:"
  echo '    export ANTHROPIC_API_KEY="sk-ant-..."'
  exit 1
fi

python3 main.py
