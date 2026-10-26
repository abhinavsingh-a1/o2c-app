"""Copy the released images of one environment to the next in o2c-gitops (ADR-018).

Usage: uv run --frozen --only-dev python scripts/promote_values.py <from-dir> <to-dir> [--replicas N]

  e.g. o2c-gitops/envs/dev o2c-gitops/envs/test --replicas 1

Used by the Jenkins promotion pipelines (o2c-infra jenkins/Jenkinsfile.promote-*). For
every values file in <to-dir>, image.repository and image.digest are set to those of the
same service in <from-dir>, and replicaCount to N when given. Comments, quotes and layout
stay as they are (scripts/set_image.py), so the files pass o2c-gitops's yamllint. Prints
one line per service; exit 1 if a service of <to-dir> has no released image in
<from-dir>, so a promotion never mixes releases silently.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from set_image import DIGEST, set_image


@dataclass(frozen=True)
class Change:
    service: str
    old_digest: str
    new_digest: str
    changed: bool


def released_image(text: str) -> tuple[str, str]:
    """(image.repository, image.digest) of a values file; ValueError if it has none."""
    values = YAML(typ="safe").load(text) or {}
    image = values.get("image") or {}
    repository, digest = image.get("repository"), image.get("digest") or ""
    if not repository or not DIGEST.match(digest):
        raise ValueError(f"no released image (repository and sha256 digest): {image}")
    return repository, digest


def promote(from_dir: Path, to_dir: Path, replicas: int | None = None) -> list[Change]:
    """Set the images of every service in to_dir from from_dir; returns the changes."""
    targets = sorted(to_dir.glob("*.yaml"))
    if not targets:
        raise ValueError(f"no values files in {to_dir}")
    missing = [t.name for t in targets if not (from_dir / t.name).is_file()]
    if missing:
        raise ValueError(f"not in {from_dir}: {', '.join(missing)}")

    # Every source is checked before anything is written
    images = [released_image((from_dir / t.name).read_text("utf-8")) for t in targets]
    changes = []
    for target, (repository, digest) in zip(targets, images, strict=True):
        before = target.read_text(encoding="utf-8")
        after = set_image(before, repository, digest, replicas)
        old = (YAML(typ="safe").load(before).get("image") or {}).get("digest") or ""
        # Compare without line endings: Windows checkouts have CRLF, set_image writes LF
        changed = after.splitlines() != before.splitlines()
        if changed:
            target.write_bytes(after.encode("utf-8"))
        changes.append(Change(target.stem, old, digest, changed))
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("from_dir", type=Path)
    parser.add_argument("to_dir", type=Path)
    parser.add_argument("--replicas", type=int)
    args = parser.parse_args(argv)
    try:
        changes = promote(args.from_dir, args.to_dir, args.replicas)
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    for c in changes:
        state = "updated" if c.changed else "unchanged"
        print(f"{c.service}: {c.old_digest or '-'} -> {c.new_digest} ({state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
