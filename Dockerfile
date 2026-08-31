# Agora in a container.
#
# Why: development on Agora happens inside a meeting held in Agora. Restarting
# the process to pick up a change drops every SSE stream, every parked
# `room_wait` and every hook registration — during the meeting about the change.
# A container makes the stable instance something you rebuild beside rather than
# on top of. See CONTRIBUTING.md, "Running it in Docker".
#
# Nothing is installed and nothing is compiled: Agora is standard-library only
# (D2) and this file must not become the place that stops being true. There is
# no pip stage on purpose — if one ever appears here, the promise is gone.

FROM python:3.13-slim

# The version this image was built from. Passed in rather than written here, so
# there is one source of truth (D9: `agora/__init__.py`) and no literal to drift.
# The documented build command reads it out of the package.
ARG AGORA_VERSION=dev
LABEL org.opencontainers.image.title="agora" \
      org.opencontainers.image.version="${AGORA_VERSION}" \
      org.opencontainers.image.source="https://github.com/determlab/agora"

# Without this, stdout is block-buffered because the container has no tty and
# nothing the server prints — including the warning that the session registry is
# not mounted — reaches `docker logs` until the buffer fills or the process
# exits. A warning that is not printed is not a warning (D3).
ENV PYTHONUNBUFFERED=1
# No .pyc in a layer that is rebuilt on every code change.
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
# Only what the server serves. The hook is deliberately absent: it runs inside
# other Claude Code sessions on the host, not in here.
COPY agora/ /app/agora/
COPY static/ /app/static/

# The transcript is the record. `rooms/<id>.jsonl` is append-only, replayed on
# start, and greppable without the server — a rebuild must never take it with
# it, so it lives on a volume rather than in the image.
VOLUME ["/data/rooms"]

# 0.0.0.0 here is the *container's* interface, not the host's. D4's loopback
# boundary is preserved one layer out, by publishing to the host loopback only:
#   -p 127.0.0.1:8765:8765
# Bind 127.0.0.1 inside the container instead and nothing can reach it — the
# published port lands on the container's own loopback, which has no listener.
# Publishing to 0.0.0.0 on the host would put the chair's seat on the LAN;
# do not.
EXPOSE 8765

# --public-url is what a client should dial, which is not what the server binds:
# `claude mcp add http://0.0.0.0:8765/mcp` registers an address nothing can
# reach. Override it when the published host port is not 8765.
ENV AGORA_PUBLIC_URL=http://127.0.0.1:8765

# --no-open: there is no browser in here to open.
ENTRYPOINT ["python", "-m", "agora.server", \
            "--host", "0.0.0.0", "--root", "/data", "--no-open"]
