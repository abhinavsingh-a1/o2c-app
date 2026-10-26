"""Set image.repository and image.digest in o2c-gitops values files (ADR-016).

Usage: uv run --frozen --only-dev python scripts/set_image.py <values.yaml> <repository> <digest>

Used by the release workflow's gitops job. ruamel.yaml's round-trip mode changes only
the two values and keeps everything else byte for byte: comments and their spacing,
quotes, key order and indentation. yq normalised inline comments to one space before
the '#', which o2c-gitops's yamllint (--strict) rejects.
"""

import re
import sys
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _yaml() -> YAML:
    yaml = YAML()  # round-trip
    yaml.preserve_quotes = True
    yaml.width = 4096  # never re-wrap long lines
    yaml.indent(mapping=2, sequence=4, offset=2)  # o2c-gitops style: "  - item"
    return yaml


def set_image(text: str, repository: str, digest: str) -> str:
    """`text` (a values file) with image.repository and image.digest replaced."""
    if not DIGEST.match(digest):
        raise ValueError(f"not a sha256 digest: {digest!r}")
    yaml = _yaml()
    values = yaml.load(text)
    image = values.get("image")
    if image is None:
        raise ValueError("values file has no image: mapping")
    image["repository"] = repository
    image["digest"] = DoubleQuotedScalarString(digest)
    out = StringIO()
    yaml.dump(values, out)
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    path, repository, digest = Path(args[0]), args[1], args[2]
    text = path.read_text(encoding="utf-8")
    path.write_bytes(set_image(text, repository, digest).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
