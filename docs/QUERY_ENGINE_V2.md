# SmartMachineAI v2 Core

This package adds a deterministic industrial query engine to the existing
SmartMachineAI project. It does not call Qwen.

## Copy into the VM

Extract the ZIP and copy its contents into:

```text
~/SmartMachineAI/
```

It adds new `engine/` files and these new files:

```text
app/query_cli_v2.py
tests/test_query_engine_v2.py
README_V2.md
```

## Run

```bash
cd ~/SmartMachineAI
source venv/bin/activate

python -m py_compile engine/*.py app/query_cli_v2.py tests/test_query_engine_v2.py
python -m unittest tests.test_query_engine_v2 -v
python -m engine.metadata_migrator
python -m app.query_cli_v2
```

The migrator creates a timestamped backup of `database/config.db` before
adding structured metadata columns.

First test:

```text
What is the compressor discharge pressure?
```

This version resolves questions to configured tags. Live PLC reading will be
connected after tag resolution is tested and tuned.
