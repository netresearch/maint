"""Content-addressed archives keep repeated collection runs independently citable."""
import hashlib
import json
from datetime import datetime
from pathlib import Path


def archive_key(snapshot: dict) -> str:
    day = datetime.fromisoformat(snapshot["generated_at"].replace("Z", "+00:00")).date().isoformat()
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{day}-{hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def write_snapshot(data_dir: Path, snapshot: dict) -> str:
    key = archive_key(snapshot)
    directory = data_dir / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    # Preserve the historical daily URL; new runs always have a unique URL too.
    for name in (snapshot["generated_at"][:10], key):
        target = directory / f"{name}.json"
        if not target.exists():
            target.write_text(content, encoding="utf-8")
        elif name == key and json.loads(target.read_text(encoding="utf-8")) != snapshot:
            raise ValueError(f"Snapshot hash collision at {key}")
    return key
