# SmartMachineAI v2

Git-ready foundation for a local industrial AI platform.

Current release: `2.0.0-alpha.1`

## Current capability

The included deterministic query engine:

- Extracts industrial concepts from operator questions
- Searches configured equipment and tags
- Ranks candidates without an LLM
- Returns confidence and scoring reasons
- Migrates `database/config.db` with structured tag metadata

This release resolves a question to a configured tag. Live PLC reading,
historian queries, LLM fallback, RAG, and API services will be added in
later releases.

## Project structure

```text
SmartMachineAI/
├── ai/          Optional local LLM integration
├── app/         CLI and future API entry points
├── backups/     Local backups, excluded from Git
├── config/      Application configuration
├── database/    SQLite access and local databases
├── docs/        Technical documentation
├── engine/      Deterministic industrial query engine
├── logs/        Runtime logs, excluded from Git
├── plc/         PLC drivers and communication
├── rag/         Manuals and document retrieval
├── scripts/     Setup and Git helper scripts
├── simulator/   PLC/data simulators
└── tests/       Automated tests
```

## Install on Ubuntu VM

Copy or extract this project to:

```bash
~/SmartMachineAI
```

Then run:

```bash
cd ~/SmartMachineAI
chmod +x scripts/*.sh
./scripts/setup_project.sh
```

## Use the existing config.db

Place your existing database here:

```text
~/SmartMachineAI/database/config.db
```

Run the migration:

```bash
source venv/bin/activate
python -m engine.metadata_migrator
```

A timestamped backup is created before the schema is changed.

## Run the query engine

```bash
python -m app.query_cli_v2
```

Example:

```text
What is the compressor discharge pressure?
```

## Create the local Git repository

```bash
./scripts/init_git.sh
```

Then create an empty **private** repository on GitHub and connect it:

```bash
git remote add origin https://github.com/YOUR_USERNAME/SmartMachineAI.git
git push -u origin main
```

For stronger security, use an SSH remote:

```bash
git remote add origin git@github.com:YOUR_USERNAME/SmartMachineAI.git
git push -u origin main
```

## Important security note

The `.gitignore` excludes SQLite databases, virtual environments, logs,
backups, and local secret files. Confirm with `git status` before every push.
