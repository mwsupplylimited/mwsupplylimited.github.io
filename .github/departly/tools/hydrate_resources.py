#!/usr/bin/env python3
"""Seed working data from the complete verified public package, retaining committed baselines."""
import hashlib
import os
from pathlib import Path

from publish_data import FILES
from verify_pages import NAMES, validate_manifest


def hydrate(site, resources):
    site, resources = Path(site), Path(resources)
    manifest = validate_manifest((site / "manifest.json").read_bytes())
    mapping = {name: source for source, name in FILES}
    if set(mapping) != NAMES:
        raise ValueError("Publisher dataset mapping differs from 24-file contract")
    payloads = []
    for row in manifest["files"]:
        path = site / row["name"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected regular public dataset: " + row["name"])
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha"]:
            raise ValueError("Public baseline hash mismatch: " + row["name"])
        payloads.append((resources / mapping[row["name"]], data))
    # No writes before all 24 public files and the manifest validate.
    for path, data in payloads:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".hydrate.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    return {"version": manifest["version"], "hydratedFiles": len(payloads)}


if __name__ == "__main__":
    import json
    root = Path(__file__).resolve().parents[3]
    print(json.dumps(hydrate(root / "departly/data", root / ".github/departly/Departly/Resources")))
