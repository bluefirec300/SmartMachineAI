#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Setting up SmartMachineAI in: $PROJECT_DIR"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python -m py_compile engine/*.py app/*.py tests/*.py
python -m unittest tests.test_query_engine_v2 -v

echo
echo "Setup complete."
echo "Next:"
echo "  source venv/bin/activate"
echo "  python -m engine.metadata_migrator"
echo "  python -m app.query_cli_v2"
