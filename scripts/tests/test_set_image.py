import pytest
from set_image import main, set_image

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64

# Shaped like o2c-gitops envs/dev/<service>.yaml: comments (inline with two spaces),
# quoted strings, a block sequence indented by two, nested mappings.
VALUES = f"""# dev values for agent (shared chart o2c-infra/helm/o2c-service >= 0.4.0, ADR-017).
image:
  repository: ghcr.io/abhinavsingh-a1/o2c-agent
  digest: "{OLD}"
replicaCount: 1
command:
  - "uvicorn"
  - "agent.app:create_app"
env:
  KAFKA_BOOTSTRAP: "host.docker.internal:29092"
  KAFKA_ADDRESS_FAMILY: "v4"  # host.docker.internal also resolves to unreachable IPv6
secretFiles:
  - secretName: agent-users
    mountPath: /app/local
resources:
  limits:
    cpu: "1"
    memory: 1Gi
"""


def test_only_the_digest_line_changes():
    out = set_image(VALUES, "ghcr.io/abhinavsingh-a1/o2c-agent", NEW)
    assert out == VALUES.replace(OLD, NEW)


def test_inline_comment_keeps_two_spaces():
    # yq wrote '"v4" # ...', which o2c-gitops's yamllint --strict rejects
    out = set_image(VALUES, "ghcr.io/abhinavsingh-a1/o2c-agent", NEW)
    assert '  KAFKA_ADDRESS_FAMILY: "v4"  # host.docker.internal' in out


def test_repository_is_replaced():
    out = set_image(VALUES, "ghcr.io/other/o2c-agent", NEW)
    assert "  repository: ghcr.io/other/o2c-agent\n" in out


def test_an_empty_digest_is_filled_in_quoted():
    out = set_image(VALUES.replace(OLD, ""), "ghcr.io/abhinavsingh-a1/o2c-agent", NEW)
    assert f'  digest: "{NEW}"\n' in out


@pytest.mark.parametrize("digest", ["", "sha256:abc", "latest", "sha256:" + "A" * 64])
def test_rejects_anything_but_a_sha256_digest(digest):
    with pytest.raises(ValueError, match="sha256"):
        set_image(VALUES, "ghcr.io/abhinavsingh-a1/o2c-agent", digest)


def test_rejects_values_without_image():
    with pytest.raises(ValueError, match="image"):
        set_image("replicaCount: 1\n", "ghcr.io/abhinavsingh-a1/o2c-agent", NEW)


def test_main_rewrites_the_file_with_lf(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_bytes(VALUES.encode())

    assert main([str(path), "ghcr.io/abhinavsingh-a1/o2c-agent", NEW]) == 0

    assert path.read_bytes() == VALUES.replace(OLD, NEW).encode()


def test_main_usage(capsys):
    assert main(["only-one-argument"]) == 2
    assert "Usage" in capsys.readouterr().err
