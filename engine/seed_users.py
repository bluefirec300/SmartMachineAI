"""
Seeds login accounts for the new role-gated UI.

Idempotent - only creates accounts that don't already exist, safe to
re-run. Two groups of accounts:

1. One "admin" account (full access, including User Management).
2. The five names already referenced as placeholder people around the
   app (Service & Maintenance's PERSON_IN_CHARGE_OPTIONS) - promoted to
   real login accounts and randomly split between "engineer" and
   "operator", per explicit request. The random split is seeded
   (deterministic) so re-running this script doesn't reshuffle
   existing accounts' roles - it only affects which role a name gets
   assigned *the first time* it's created.

All accounts get the default password "123456" - meant to be changed
after first login once the UI has a "change my password" control.
"""

from __future__ import annotations

import random

from config.user_manager import DEFAULT_DATABASE_PATH, UserManager


DEFAULT_PASSWORD = "123456"

# Same five names as ui/pages/4_Service_and_Maintenance.py's
# PERSON_IN_CHARGE_OPTIONS - that page now reads from the users table
# instead of this hardcoded list (see get_person_in_charge_options in
# ui/data_access.py).
PLACEHOLDER_NAMES = [
    "Ahmad Faizal",
    "Siti Nurhaliza",
    "Kumar Raj",
    "Wong Mei Ling",
    "Tan Wei Jian",
]


def _username_for(display_name: str) -> str:
    return display_name.strip().lower().replace(" ", ".")


def seed(database_path=DEFAULT_DATABASE_PATH) -> dict[str, list[str]]:
    manager = UserManager(database_path=database_path)
    created: list[str] = []
    skipped: list[str] = []

    if manager.get_user("admin") is None:
        manager.create_user(
            username="admin",
            display_name="Administrator",
            password=DEFAULT_PASSWORD,
            role="admin",
            created_by="system",
        )
        created.append("admin")
    else:
        skipped.append("admin")

    # Seeded RNG - deterministic assignment, not tied to wall-clock
    # randomness, so re-running this script is reproducible.
    rng = random.Random("smartmachineai-user-seed")
    roles = ["engineer", "operator"]

    for name in PLACEHOLDER_NAMES:
        username = _username_for(name)

        if manager.get_user(username) is not None:
            skipped.append(username)
            continue

        role = rng.choice(roles)
        manager.create_user(
            username=username,
            display_name=name,
            password=DEFAULT_PASSWORD,
            role=role,
            created_by="system",
        )
        created.append(f"{username} ({role})")

    return {"created": created, "skipped": skipped}


if __name__ == "__main__":
    result = seed()

    if result["created"]:
        print("Created:")
        for entry in result["created"]:
            print(f"  {entry}")

    if result["skipped"]:
        print("Already existed, skipped:")
        for entry in result["skipped"]:
            print(f"  {entry}")

    if not result["created"] and not result["skipped"]:
        print("Nothing to do.")
