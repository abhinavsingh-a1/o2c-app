from intake import gunicorn_conf


def test_no_control_socket_on_a_read_only_root_filesystem():
    # gunicorn >= 26 would create $HOME/.gunicorn/gunicorn.ctl (ADR-017)
    assert gunicorn_conf.control_socket_disable is True


def test_on_starting_empties_the_multiprocess_directory(monkeypatch, tmp_path):
    directory = tmp_path / "prometheus"
    directory.mkdir()
    (directory / "counter_1.db").write_bytes(b"old")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(directory))

    gunicorn_conf.on_starting(server=None)

    assert directory.is_dir()
    assert list(directory.iterdir()) == []
