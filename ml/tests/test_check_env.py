import os
import shutil
import subprocess
import sys

import check_env
import pytest
from check_env import LineError, check, main, parse_line

SECRET = "s3cr$t#val ue"  # must never appear in the output


def write(tmp_path, text: str, name: str = ".env"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("A=1", ("A", "", "1")),
        ("export B='x y'", ("B", "'", "x y")),
        ('C="z" # comment', ("C", '"', "z")),
        ("  # comment", None),
        ("", None),
    ],
)
def test_parse_line(line, parsed):
    assert parse_line(line) == parsed


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("NO_EQUALS", "no '='"),
        ("1BAD=x", "invalid key"),
        ("OPEN='abc", "never closed"),
        ('AFTER="a"b', "text after"),
    ],
)
def test_unparsable_lines(line, message):
    with pytest.raises(LineError, match=message):
        parse_line(line)


def test_findings_name_line_and_key_but_never_the_value(tmp_path, capsys):
    env = write(
        tmp_path,
        "\n".join(
            [
                "OK=plain-value_1",
                f"QUOTED='{SECRET}'",
                f"UNQUOTED={SECRET}",
                f'DOUBLE="{SECRET}"',
                "BROKEN='never closed",
                "OK=again",
                "PLACE=replace-me",
            ]
        ),
    )

    findings = {(f.level, f.line, f.key) for f in check(env)}

    assert ("WARN", 3, "UNQUOTED") in findings
    assert ("WARN", 4, "DOUBLE") in findings  # $ in double quotes
    assert ("ERROR", 5, "BROKEN") in findings
    assert ("WARN", 6, "OK") in findings  # duplicate
    assert ("INFO", 7, "PLACE") in findings
    assert not any(f[2] == "QUOTED" for f in findings)  # single quotes are literal

    assert main([str(env)]) == 1  # an ERROR
    assert SECRET not in capsys.readouterr().out


def test_url_values_with_reserved_characters_are_flagged(tmp_path):
    compose = write(
        tmp_path,
        "x:\n  URL: postgresql://${DB_USER}:${DB_PASSWORD}@postgres:5432/o2c\n"
        "  OTHER: ${API_KEY}\n",
        "compose.yml",
    )
    env = write(tmp_path, "DB_PASSWORD='p@ss'\nAPI_KEY='p@ss'\nDB_USER=o2c\n")

    findings = {(f.level, f.key) for f in check(env, compose=compose)}

    assert ("WARN", "DB_PASSWORD") in findings
    assert ("WARN", "API_KEY") not in findings  # not used in a URL
    assert check_env.url_keys(compose) == {"DB_USER", "DB_PASSWORD"}


def test_missing_keys_from_the_example(tmp_path):
    example = write(tmp_path, "A=replace-me\nB=replace-me\n", ".env.example")
    env = write(tmp_path, "A=1\n")

    assert [f.key for f in check(env, example=example) if f.level == "INFO"] == ["B"]


def test_the_real_env_example_parses():
    example = check_env.REPO_ROOT / ".env.example"
    assert not [f for f in check(example) if f.level == "ERROR"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_uv_run_reads_single_quoted_special_values_literally(tmp_path):
    """What Makefile targets such as dbt-build rely on (uv run --env-file .env)."""
    env = write(tmp_path, f"SPECIAL='{SECRET}'\n")
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--env-file",
            env.as_posix(),  # uv drops backslashes from this path on Windows
            sys.executable,
            "-c",
            "import os; print(os.environ['SPECIAL'] == " + repr(SECRET) + ")",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "UV_NO_SYNC": "1"},
        timeout=120,
    )
    assert result.stdout.strip() == "True", result.stderr
