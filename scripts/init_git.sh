#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -d ".git" ]; then
    echo "Git repository already exists."
    exit 0
fi

git init
git branch -M main
git add .
git commit -m "Initial SmartMachineAI v2 project"

echo
echo "Git repository initialized."
echo "Create a private empty GitHub repository, then run:"
echo "  git remote add origin https://github.com/YOUR_USERNAME/SmartMachineAI.git"
echo "  git push -u origin main"
