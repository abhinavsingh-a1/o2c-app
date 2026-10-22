"""Push prompts/*.md to Langfuse prompt management (ADR-010).

Usage: uv run --group ml python ml/sync_prompts.py

Each file becomes a text prompt named after the file stem, labelled "local" and with the
git short sha of HEAD. Git stays the source of truth and the services keep reading the
files; Langfuse gets a browsable, versioned copy that traces can be compared against.
If the text under the "local" label is already identical, no new version is created; the
sha label is added to that version instead.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
LOCAL = "local"
LATEST = "latest"


def prompt_files(prompts_dir: Path = PROMPTS_DIR) -> dict[str, str]:
    """name (file stem) -> text of every prompts/*.md."""
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(prompts_dir.glob("*.md"))
    }


def current_local(client, name: str):
    """The version labelled "local", or None if the prompt does not exist yet."""
    # The raw API: the SDK's get_prompt caches and retries, and hung for a missing prompt.
    try:
        return client.client.prompts.get(name, label=LOCAL)
    except Exception:  # noqa: BLE001 - NotFoundError when the prompt does not exist
        return None


def sync(client, name: str, text: str, sha: str) -> str:
    """Create a new version (labels local + sha) unless the local version has this text.
    Returns "created", "labelled" or "unchanged"."""
    current = current_local(client, name)
    if current is not None and current.prompt == text:
        if sha in current.labels:
            return "unchanged"
        # "latest" is managed by Langfuse and may not be sent.
        labels = [label for label in current.labels if label != LATEST]
        client.update_prompt(
            name=name, version=current.version, new_labels=[*labels, sha]
        )
        return "labelled"
    client.create_prompt(
        name=name,
        prompt=text,
        labels=[LOCAL, sha],
        type="text",
        commit_message=f"prompts/{name}.md at {sha}",
    )
    return "created"


def main() -> dict[str, str]:
    from langfuse_common import git_short_sha, make_langfuse

    client, sha = make_langfuse(), git_short_sha()
    results = {}
    for name, text in prompt_files().items():
        results[name] = sync(client, name, text, sha)
        print(f"{name}: {results[name]} ({LOCAL}, {sha})")
    client.flush()
    return results


if __name__ == "__main__":
    main()
