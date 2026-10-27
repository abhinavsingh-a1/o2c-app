"""DO NOT MERGE: gate evidence (security / Semgrep). Never imported or run.

Passes a command string to a shell on purpose, so Semgrep (p/python, severity ERROR)
reports subprocess-shell-true.
"""

import subprocess
import sys


def run(command: str) -> None:
    subprocess.run(command, shell=True, check=True)


if __name__ == "__main__":
    run(sys.argv[1])
