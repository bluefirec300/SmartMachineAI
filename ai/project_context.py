from pathlib import Path


PROJECT_ROOT = Path.home() / "SmartMachineAI"

ALLOWED_EXTENSIONS = {
    ".py",
    ".json",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
    ".service",
}

EXCLUDED_DIRECTORIES = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "node_modules",
    "backups",
    "logs",
}

EXCLUDED_FILES = {
    ".openai_env",
    ".env",
    "machine_data.db",
}

MAX_FILE_SIZE = 100_000
MAX_TOTAL_CHARACTERS = 80_000


def should_include_file(file_path):
    """Return True when a file is safe and useful for project context."""

    if file_path.name in EXCLUDED_FILES:
        return False

    if file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        return False

    if any(part in EXCLUDED_DIRECTORIES for part in file_path.parts):
        return False

    try:
        if file_path.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False

    return True


def get_project_files():
    """Return a sorted list of project files suitable for AI context."""

    files = []

    for file_path in PROJECT_ROOT.rglob("*"):
        if file_path.is_file() and should_include_file(file_path):
            files.append(file_path)

    return sorted(files)


def read_project_file(file_path):
    """Read a text file safely."""

    try:
        return file_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return f"[Unable to read file: {error}]"


def build_project_context():
    """Build one text block containing the project source files."""

    sections = []
    total_characters = 0

    for file_path in get_project_files():
        relative_path = file_path.relative_to(PROJECT_ROOT)
        content = read_project_file(file_path)

        section = (
            f"\n\n===== FILE: {relative_path} =====\n"
            f"{content}"
        )

        if total_characters + len(section) > MAX_TOTAL_CHARACTERS:
            remaining = MAX_TOTAL_CHARACTERS - total_characters

            if remaining > 200:
                sections.append(
                    section[:remaining]
                    + "\n[Project context truncated here.]"
                )

            break

        sections.append(section)
        total_characters += len(section)

    return "".join(sections).strip()


def print_project_summary():
    """Display files that will be included in the context."""

    files = get_project_files()

    print(f"Project root: {PROJECT_ROOT}")
    print(f"Included files: {len(files)}")

    for file_path in files:
        relative_path = file_path.relative_to(PROJECT_ROOT)
        print(f"- {relative_path}")


if __name__ == "__main__":
    print_project_summary()

    context = build_project_context()

    print()
    print(f"Context size: {len(context):,} characters")
