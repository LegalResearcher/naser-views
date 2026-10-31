"""Persistent circuit breaker for account, login, or balance failures."""
import json
import os

PAUSE_FILENAME = "account_pause.json"


def pause_path(state_dir):
    return os.path.join(state_dir, PAUSE_FILENAME)


def is_paused(state_dir):
    path = pause_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as file:
            value = json.load(file)
        return isinstance(value, dict) and value.get("paused") is True
    except (OSError, ValueError):
        return False


def pause_account_processing(state_dir, reason):
    os.makedirs(state_dir, exist_ok=True)
    path = pause_path(state_dir)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump({"paused": True, "reason": reason}, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write("account_fatal=true\n")
