"""Publish the @champion models as OCI artifacts and pin them in models.lock.json (ADR-016).

Usage: uv run python ml/publish_models.py [--registry ghcr.io/abhinavsingh-a1]
       [--lock models.lock.json] [--plain-http]          (make models-publish)

For o2c-doc-classifier and o2c-quantity-anomaly:
1. resolve the @champion version in MLflow (MLFLOW_TRACKING_URI, default
   http://localhost:5000);
2. if <registry>/o2c-model-<name>:v<version> exists, keep its digest: a registered model
   version never changes, so it is pushed only once;
3. otherwise download that version (models:/<name>/<version>, not the moving alias) and
   push the MLflow model directory with oras. The source annotation links the package to
   this repo, so the release workflow's GITHUB_TOKEN can pull it;
4. write the lock file: name, version, repository, tag and digest. The images pull the
   artifact by digest (docker/python-service.Dockerfile, release.yml).

Login: the token of `gh auth token` goes to oras on stdin for each call and is never
stored; it needs the write:packages scope (gh auth refresh -s write:packages).
--plain-http is for a local registry rehearsal and uses no login.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ["o2c-doc-classifier", "o2c-quantity-anomaly"]
REGISTRY = "ghcr.io/abhinavsingh-a1"
LOCK_FILE = ROOT / "models.lock.json"
ARTIFACT_TYPE = "application/vnd.o2c.mlflow-model.v1"
SOURCE = "https://github.com/abhinavsingh-a1/o2c-app"


def repository(registry: str, name: str) -> str:
    """ghcr.io/abhinavsingh-a1 + o2c-doc-classifier -> .../o2c-model-doc-classifier"""
    return f"{registry}/o2c-model-{name.removeprefix('o2c-')}"


class Oras:
    """The oras CLI. With a password (a token), it is passed on stdin to every call."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        plain_http: bool = False,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.username, self.password = username, password
        self.plain_http, self.run = plain_http, run

    def _call(self, args: list[str], cwd: Path | None = None):
        cmd = ["oras", *args]
        if self.plain_http:
            cmd.append("--plain-http")
        if self.password is not None:
            cmd += ["--username", self.username or "oauth2", "--password-stdin"]
        return self.run(
            cmd,
            input=self.password or "",
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def resolve(self, ref: str) -> str | None:
        """The manifest digest of `ref`, or None if it does not exist."""
        result = self._call(["resolve", ref])
        return result.stdout.strip() if result.returncode == 0 else None

    def push(self, ref: str, directory: Path, annotations: dict[str, str]) -> str:
        """Push `directory` (as one tar layer named after it); returns the digest."""
        args = ["push", ref, f"{directory.name}/", "--artifact-type", ARTIFACT_TYPE]
        for key, value in sorted(annotations.items()):
            args += ["--annotation", f"{key}={value}"]
        result = self._call([*args, "--format", "json"], cwd=directory.parent)
        if result.returncode != 0:
            raise RuntimeError(f"oras push {ref} failed: {result.stderr.strip()}")
        return json.loads(result.stdout)["digest"]


def champion_version(client, name: str) -> str:
    return str(client.get_model_version_by_alias(name, "champion").version)


def download_version(name: str, version: str, workdir: Path) -> Path:
    """The MLflow model directory of name/version, as workdir/<name>."""
    import mlflow

    downloaded = mlflow.artifacts.download_artifacts(
        artifact_uri=f"models:/{name}/{version}", dst_path=str(workdir / "download")
    )
    target = workdir / name
    shutil.move(downloaded, target)
    return target


def publish(
    name: str,
    version: str,
    registry: str,
    oras: Oras,
    download: Callable[[str, str, Path], Path] = download_version,
) -> tuple[dict, bool]:
    """(lock entry, pushed): pushes only if the version's tag does not exist yet."""
    repo = repository(registry, name)
    tag = f"v{version}"
    digest = oras.resolve(f"{repo}:{tag}")
    pushed = digest is None
    if pushed:
        with tempfile.TemporaryDirectory() as tmp:
            directory = download(name, version, Path(tmp))
            digest = oras.push(
                f"{repo}:{tag}",
                directory,
                {
                    "org.opencontainers.image.source": SOURCE,
                    "org.opencontainers.image.title": name,
                    "org.opencontainers.image.version": version,
                    "org.opencontainers.image.description": (
                        f"MLflow model {name} version {version}"
                    ),
                },
            )
    entry = {
        "name": name,
        "version": version,
        "repository": repo,
        "tag": tag,
        "digest": digest,
    }
    return entry, pushed


def write_lock(path: Path, entries: list[dict]) -> None:
    lock = {"models": sorted(entries, key=lambda e: e["name"])}
    path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def gh_token() -> str:
    token = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not token:
        raise RuntimeError("gh auth token returned nothing: run gh auth login")
    return token


def main(argv: list[str] | None = None, client=None, oras: Oras | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--lock", type=Path, default=LOCK_FILE)
    parser.add_argument(
        "--plain-http", action="store_true", help="local registry, no login"
    )
    args = parser.parse_args(argv)
    if client is None:
        import mlflow

        # Global: the registry and download_artifacts (models:/) both use it
        mlflow.set_tracking_uri(
            os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
        )
        client = mlflow.MlflowClient()
    if oras is None:
        if args.plain_http:
            oras = Oras(plain_http=True)
        else:
            owner = args.registry.split("/")[1]
            oras = Oras(username=owner, password=gh_token())

    entries = []
    for name in MODELS:
        entry, pushed = publish(
            name, champion_version(client, name), args.registry, oras
        )
        entries.append(entry)
        state = "pushed" if pushed else "already published"
        print(
            f"{name} v{entry['version']}: {state} "
            f"{entry['repository']}@{entry['digest']}"
        )
    write_lock(args.lock, entries)
    print(f"wrote {args.lock}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
