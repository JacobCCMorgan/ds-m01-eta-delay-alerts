# Fixed Linux/amd64 runtime for the M3b.2 replay protocol.
FROM docker.io/library/python@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-linux-amd64.lock /opt/ds-m01/requirements-linux-amd64.lock
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-index --only-binary=:all: --require-hashes -r /opt/ds-m01/requirements-linux-amd64.lock \
    && python -m pip check
WORKDIR /
