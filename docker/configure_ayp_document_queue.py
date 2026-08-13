#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

QUEUE_NAME = "documents"
QUEUE_CONFIG = {"timeout": 600, "background_workers": 1}


def configure(path: Path) -> dict:
	path.parent.mkdir(parents=True, exist_ok=True)
	lock_path = path.with_name(f".{path.name}.ayp-workers.lock")
	with lock_path.open("a+", encoding="utf-8") as lock:
		fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
		data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
		workers = data.get("workers")
		if workers is None:
			workers = {}
		if not isinstance(workers, dict):
			raise ValueError("common_site_config workers must be a JSON object")
		workers[QUEUE_NAME] = dict(QUEUE_CONFIG)
		data["workers"] = workers
		original_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
		fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
		try:
			with os.fdopen(fd, "w", encoding="utf-8") as handle:
				json.dump(data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
				handle.write("\n")
				handle.flush()
				os.fsync(handle.fileno())
			os.chmod(temporary_name, original_mode)
			os.replace(temporary_name, path)
		finally:
			if os.path.exists(temporary_name):
				os.unlink(temporary_name)
		fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
	return {"queue": QUEUE_NAME, **QUEUE_CONFIG}


def main() -> None:
	target = Path(
		sys.argv[1] if len(sys.argv) > 1 else "/home/frappe/frappe-bench/sites/common_site_config.json"
	)
	result = configure(target)
	print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
	main()
