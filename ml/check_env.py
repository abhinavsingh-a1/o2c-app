"""Check .env for lines the tools would read differently - without printing any value.

Usage: uv run python ml/check_env.py [path, default .env]

Three parsers read .env: docker compose, `uv run --env-file` (Makefile targets such as
dbt-build) and python-dotenv (pydantic-settings in the services). They agree on
single-quoted values (taken literally) and on plain values without special
characters; they disagree on `$`, `#`, spaces and backslashes in unquoted or
double-quoted values (compose and uv expand `$VAR` and cut at ` #`, python-dotenv
does not). Rule: put any value with special characters in single quotes, e.g.
    POSTGRES_PASSWORD='p@ss#w$rd'

Reports, by line number and key only:
  ERROR  unparsable lines (no `=`, invalid key, unclosed quote, text after the quote)
  WARN   values the parsers read differently (quote them with single quotes),
         duplicate keys, values used inside URLs in docker-compose.yml that contain
         URL-reserved characters (they break e.g. postgresql://user:password@host)
  INFO   keys of .env.example missing from .env, values still `replace-me`
Exits 1 if there is any ERROR.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Characters that compose/uv and python-dotenv treat differently outside single quotes
AMBIGUOUS = set("$#\\\"' \t`")
# Characters that break a URL when inserted unencoded (user:password@host/db?x#y)
URL_RESERVED = set("@:/?#%[] ")
COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
PLACEHOLDER = "replace-me"


@dataclass(frozen=True)
class Finding:
    level: str  # ERROR, WARN, INFO
    line: int  # 0: not about one line
    key: str
    message: str

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line else "file"
        return f"{self.level:5} {where:>8}  {self.key or '-'}: {self.message}"


class LineError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(message)
        self.key, self.message = key, message


def parse_line(text: str) -> tuple[str, str, str] | None:
    """None for blank and comment lines, else (key, quote, value); quote is "'", '"' or
    "" (unquoted). LineError if the line cannot be parsed."""
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        raise LineError("", "no '=' (not KEY=value)")
    key, rest = stripped.split("=", 1)
    key = key.strip()
    if not KEY.match(key):
        raise LineError(
            "", "invalid key (letters, digits and _, not starting with a digit)"
        )
    rest = rest.lstrip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise LineError(key, f"opening {quote} is never closed on this line")
        after = rest[end + 1 :].strip()
        if after and not after.startswith("#"):
            raise LineError(key, f"text after the closing {quote}")
        return key, quote, rest[1:end]
    return key, "", rest


def ambiguity(quote: str, value: str) -> str | None:
    if quote == "'":
        return None  # literal for all three parsers
    if quote == '"':
        if "$" in value or "\\" in value:
            return "$ or \\ in double quotes is expanded by compose/uv but not by python-dotenv"
        return None
    if " #" in value or "\t#" in value:
        return "' #' starts a comment for compose/uv: the value is cut there"
    if set(value) & AMBIGUOUS:
        return "special characters in an unquoted value are read differently"
    return None


def url_keys(compose: Path) -> set[str]:
    """Variables that docker-compose.yml inserts into a URL (a line with ://)."""
    if not compose.exists():
        return set()
    return {
        name
        for line in compose.read_text(encoding="utf-8").splitlines()
        if "://" in line
        for name in COMPOSE_VAR.findall(line)
    }


def check(
    env: Path, example: Path | None = None, compose: Path | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    values: dict[str, str] = {}
    in_urls = url_keys(compose) if compose else set()
    for number, text in enumerate(env.read_text(encoding="utf-8").splitlines(), 1):
        try:
            parsed = parse_line(text)
        except LineError as error:
            findings.append(Finding("ERROR", number, error.key, error.message))
            continue
        if parsed is None:
            continue
        key, quote, value = parsed
        if key in seen:
            findings.append(
                Finding("WARN", number, key, f"duplicate (also line {seen[key]})")
            )
        seen[key] = number
        values[key] = value
        problem = ambiguity(quote, value)
        if problem:
            findings.append(
                Finding("WARN", number, key, f"{problem}; use single quotes")
            )
        if key in in_urls and set(value) & URL_RESERVED:
            findings.append(
                Finding(
                    "WARN",
                    number,
                    key,
                    "contains URL-reserved characters (@ : / ? # % [ ] space) and "
                    "docker-compose.yml puts it into a URL: choose another value or "
                    "percent-encode it",
                )
            )
        if PLACEHOLDER in value:
            findings.append(Finding("INFO", number, key, "still the placeholder"))
    if example and example.exists():
        for key in check_keys(example):
            if key not in values:
                findings.append(
                    Finding("INFO", 0, key, "in .env.example, missing here")
                )
    return findings


def check_keys(path: Path) -> list[str]:
    keys = []
    for text in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed = parse_line(text)
        except LineError:
            continue
        if parsed:
            keys.append(parsed[0])
    return keys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env = Path(args[0]) if args else REPO_ROOT / ".env"
    if not env.exists():
        print(f"{env} not found (copy .env.example to .env)")
        return 1
    findings = check(env, REPO_ROOT / ".env.example", REPO_ROOT / "docker-compose.yml")
    for finding in findings:
        print(finding)
    errors = sum(f.level == "ERROR" for f in findings)
    warnings = sum(f.level == "WARN" for f in findings)
    print(f"{env.name}: {errors} errors, {warnings} warnings (values are never shown)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
