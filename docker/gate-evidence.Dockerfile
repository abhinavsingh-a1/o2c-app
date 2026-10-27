# DO NOT MERGE: gate evidence (security / Checkov). Never built by any workflow.
# Runs as root on purpose, so Checkov fails CKV_DOCKER_8 ("the last USER is not root").
FROM alpine:3.22
USER root
