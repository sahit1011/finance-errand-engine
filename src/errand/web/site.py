"""Writes the Results and Replay pages as plain files for GitHub Pages. Live chat needs the local server, so it stays out."""

import json
import shutil
from pathlib import Path

from errand.web import data
from errand.web.server import STATIC_DIR, STATIC_FILES


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def export_site(repo: Path, out: Path) -> None:
    # Read everything first, so that a failure leaves the published copy as it was.
    results = data.load_results(repo)
    replays = data.list_replays()
    runs = {replay["id"]: data.load_replay(repo, replay["id"]) for replay in replays["replays"]}
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, _ in STATIC_FILES.values():
        shutil.copyfile(STATIC_DIR / name, out / name)
    _write_json(out / "data" / "results.json", results)
    _write_json(out / "data" / "replays.json", replays)
    for replay_id, run in runs.items():
        _write_json(out / "data" / "replays" / f"{replay_id}.json", run)
