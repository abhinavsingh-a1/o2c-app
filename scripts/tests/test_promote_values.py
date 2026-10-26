import pytest
from promote_values import Change, main, promote, released_image

DEV = "sha256:" + "d" * 64
TEST = "sha256:" + "e" * 64


def values(env: str, service: str, digest: str, replicas: int) -> str:
    # Shaped like o2c-gitops envs/<env>/<service>.yaml
    return f"""# {env} values for {service}
image:
  repository: ghcr.io/abhinavsingh-a1/o2c-{service}
  digest: "{digest}"
replicaCount: {replicas}
o2cEnv: {env}
env:
  KAFKA_ADDRESS_FAMILY: "v4"  # host.docker.internal also resolves to unreachable IPv6
"""


@pytest.fixture
def envs(tmp_path):
    dev, test = tmp_path / "dev", tmp_path / "test"
    dev.mkdir()
    test.mkdir()
    for service in ("agent", "intake"):
        (dev / f"{service}.yaml").write_text(values("dev", service, DEV, 1), "utf-8")
        (test / f"{service}.yaml").write_text(values("test", service, TEST, 0), "utf-8")
    return dev, test


def test_released_image():
    assert released_image(values("dev", "agent", DEV, 1)) == (
        "ghcr.io/abhinavsingh-a1/o2c-agent",
        DEV,
    )


@pytest.mark.parametrize(
    "text",
    [
        "replicaCount: 1\n",
        "image:\n  repository: ghcr.io/x/o2c-agent\n",
        'image:\n  repository: ghcr.io/x/o2c-agent\n  digest: ""\n',
        "image:\n  digest: sha256:" + "d" * 64 + "\n",
        "",
    ],
)
def test_released_image_rejects_values_without_a_released_image(text):
    with pytest.raises(ValueError, match="no released image"):
        released_image(text)


def test_promote_sets_digests_and_replicas_and_keeps_the_rest(envs):
    dev, test = envs

    changes = promote(dev, test, replicas=1)

    assert changes == [
        Change("agent", TEST, DEV, True),
        Change("intake", TEST, DEV, True),
    ]
    expected = values("test", "agent", DEV, 1)
    assert (test / "agent.yaml").read_text("utf-8") == expected


def test_promote_without_replicas_keeps_them(envs):
    dev, test = envs
    promote(dev, test)
    assert "replicaCount: 0\n" in (test / "agent.yaml").read_text("utf-8")


def test_promote_twice_changes_nothing_the_second_time(envs):
    dev, test = envs
    promote(dev, test, replicas=1)
    assert [c.changed for c in promote(dev, test, replicas=1)] == [False, False]


def test_unchanged_crlf_file_is_not_rewritten(envs):
    dev, test = envs
    promote(dev, test, replicas=1)
    target = test / "agent.yaml"
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))

    promote(dev, test, replicas=1)

    assert b"\r\n" in target.read_bytes()


def test_a_service_missing_in_the_source_fails(envs):
    dev, test = envs
    (dev / "intake.yaml").unlink()
    with pytest.raises(ValueError, match="intake.yaml"):
        promote(dev, test)
    assert "e" * 64 in (test / "agent.yaml").read_text("utf-8")  # nothing written


def test_an_empty_target_fails(tmp_path, envs):
    dev, _ = envs
    with pytest.raises(ValueError, match="no values files"):
        promote(dev, tmp_path)


def test_main_prints_one_line_per_service(envs, capsys):
    dev, test = envs
    assert main([str(dev), str(test), "--replicas", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"agent: {TEST} -> {DEV} (updated)",
        f"intake: {TEST} -> {DEV} (updated)",
    ]


def test_main_fails_on_an_unreleased_source(envs, capsys):
    dev, test = envs
    (dev / "agent.yaml").write_text("image:\n  repository: x\n", "utf-8")
    assert main([str(dev), str(test)]) == 1
    assert "FAIL" in capsys.readouterr().err


def test_an_unreleased_source_writes_nothing(envs):
    dev, test = envs
    (dev / "intake.yaml").write_text("image:\n  repository: x\n", "utf-8")
    with pytest.raises(ValueError, match="no released image"):
        promote(dev, test, replicas=1)
    assert "e" * 64 in (test / "agent.yaml").read_text("utf-8")
