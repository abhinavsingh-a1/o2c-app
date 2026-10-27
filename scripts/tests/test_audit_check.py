"""scripts/audit_check.sh with stub tools: the real gh, docker, uv and verify_image.sh are
replaced by small scripts on PATH, and o2c-gitops by a temporary git repository."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit_check.sh"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def find_bash() -> str | None:
    """Git Bash on Windows (System32\\bash.exe is WSL and sees other paths), else bash."""
    if os.name == "nt":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git"
            / "bin"
            / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
        return None
    return shutil.which("bash")


BASH = find_bash()
pytestmark = pytest.mark.skipif(
    BASH is None or not shutil.which("git"), reason="needs bash and git"
)


def stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def values(digest: str | None) -> str:
    line = f'  digest: "{digest}"\n' if digest else '  digest: ""\n'
    return f"image:\n  repository: ghcr.io/o/o2c-agent\n{line}replicaCount: 1\n"


def gitops(tmp_path: Path, prod: str | None, test: str | None) -> Path:
    repo = tmp_path / "gitops"
    for env, digest in (("prod", prod), ("test", test)):
        (repo / "envs" / env).mkdir(parents=True)
        (repo / "envs" / env / "agent.yaml").write_text(
            values(digest), encoding="utf-8"
        )
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "values"],
        check=True,
    )
    return repo


def run(
    tmp_path: Path,
    prod: str | None = DIGEST,
    test: str | None = OTHER,
    verify_ok: bool = True,
    gh_ok: bool = True,
    docker_ok: bool = True,
    chain_ok: bool = True,
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    verified = tmp_path / "verified.txt"
    stub(
        bin_dir,
        "verify_image",
        f'echo "$1" >> "{verified.as_posix()}"; '
        + ("echo PASS; exit 0" if verify_ok else "echo FAIL; exit 1"),
    )
    stub(
        bin_dir,
        "gh",
        'echo "2026-10-27T14:38:15Z  #23  chore(prod): promote o2c-app 36a638c  (merged by u)"; '
        'echo "2026-10-27T13:16:15Z  #22  revert(prod): roll back promotion e8b038f  (merged by u)"'
        if gh_ok
        else 'echo "gh: not logged in" >&2; exit 1',
    )
    stub(
        bin_dir,
        "docker",
        'echo "file: audit-trail.log.0"; echo "proceedEmpty by approver"'
        if docker_ok
        else 'echo "No such container" >&2; exit 1',
    )
    stub(
        bin_dir,
        "uv",
        'echo "chain intact: 3 entries in 1 objects, head seq 3 hash abc..."'
        if chain_ok
        else 'echo "BROKEN after 2 valid entries: 2026/10/27.jsonl line 3"; exit 1',
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "GITOPS_DIR": gitops(tmp_path, prod, test).as_posix(),
        "GITOPS_REF": "HEAD",
        "GITOPS_FETCH": "0",
        "VERIFY_IMAGE": (bin_dir / "verify_image").as_posix(),
    }
    if os.name == "nt":  # Git Bash: its own PATH syntax; prepend the stub dir there
        drive, rest = bin_dir.as_posix().split(":", 1)
        env["STUB_BIN"] = f"/{drive.lower()}{rest}"
    result = subprocess.run(
        [
            BASH,
            "-c",
            'PATH="${STUB_BIN:+$STUB_BIN:}$PATH" exec bash "$0"',
            SCRIPT.as_posix(),
        ],
        env=env,
        check=False,  # the exit code is asserted by the tests
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    result.verified = verified.read_text().split() if verified.exists() else []
    return result


def summary(result) -> dict[str, str]:
    lines = result.stdout.split("== Summary ==")[1].splitlines()
    return {
        line[7:35].strip(): line[:6].strip()
        for line in lines
        if line.startswith(("PASS", "FAIL"))
    }


def test_all_checks_pass(tmp_path):
    result = run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "AUDIT CHECK: PASS" in result.stdout
    assert result.verified == [f"ghcr.io/o/o2c-agent@{DIGEST}"]
    assert summary(result) == {
        "image prod/agent": "PASS",
        "promotion history": "PASS",
        "jenkins audit trail": "PASS",
        "audit chain": "PASS",
    }
    assert "1 promotions, 1 rollbacks" in result.stdout


def test_falls_back_to_test_when_prod_has_no_digest(tmp_path):
    result = run(tmp_path, prod=None)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.verified == [f"ghcr.io/o/o2c-agent@{OTHER}"]
    assert "image test/agent" in summary(result)


def test_no_digest_anywhere_fails(tmp_path):
    result = run(tmp_path, prod=None, test=None)

    assert result.returncode == 1
    assert summary(result)["images"] == "FAIL"


@pytest.mark.parametrize(
    ("failure", "check"),
    [
        ({"verify_ok": False}, "image prod/agent"),
        ({"gh_ok": False}, "promotion history"),
        ({"docker_ok": False}, "jenkins audit trail"),
        ({"chain_ok": False}, "audit chain"),
    ],
)
def test_any_failing_check_fails_the_run(tmp_path, failure, check):
    result = run(tmp_path, **failure)

    assert result.returncode == 1
    assert "AUDIT CHECK: FAIL" in result.stdout
    checks = summary(result)
    assert checks[check] == "FAIL"
    assert [c for c, status in checks.items() if status == "FAIL"] == [check]
