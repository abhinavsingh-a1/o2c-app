"""gunicorn settings for intake: prometheus_client multiprocess mode (ADR-012).

With several workers, each worker writes its metric values to files in
PROMETHEUS_MULTIPROC_DIR and GET /metrics adds them up (o2c_common.metrics). The
directory is emptied when gunicorn starts, so values of an earlier run are not kept,
and a dead worker's live values are removed.

Usage: gunicorn -c python:intake.gunicorn_conf "intake.app:create_app()"
"""

import os
import shutil

from prometheus_client import multiprocess

bind = "0.0.0.0:8200"
workers = 2
# gunicorn >= 26 opens a control socket under $HOME by default. Intake does not use it,
# and $HOME is read-only in Kubernetes (readOnlyRootFilesystem, ADR-017).
control_socket_disable = True


def on_starting(server) -> None:
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if directory:
        shutil.rmtree(directory, ignore_errors=True)
        os.makedirs(directory, exist_ok=True)


def child_exit(server, worker) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.mark_process_dead(worker.pid)
