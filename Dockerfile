# The always-on gateway (`cagentic --serve`) as a container.
#
# Same daemon the launchd/systemd service runs — see cagentic/service.py — but
# with the machine boundary drawn by the container instead of by the login
# session. Two consequences shape this file:
#
#   * It must bind 0.0.0.0 to be reachable at all, so it sets
#     CAGENTIC_GATEWAY_BIND rather than gateway.lan: `lan` also turns off the
#     anti-DNS-rebinding Host check, and a container published to
#     127.0.0.1 has no reason to give that up. Reachability is the publish
#     rule's job (see docker-compose.yml), not the bind address's.
#   * bubblewrap is deliberately NOT installed. `bwrap` needs unprivileged
#     user namespaces that most container runtimes withhold, so it would be
#     present-but-broken — every approved run_bash failing for a reason the
#     user can't see. Without it sandbox.backend() honestly reports "none"
#     and the container itself is the boundary.

FROM python:3.12-slim

# git: the coding agent shells out to it. ca-certificates: provider HTTPS.
# tini: the gateway spawns children (hooks, run_bash, MCP servers), and PID 1
# without a reaper leaves them as zombies for the life of the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root: this process runs shell commands on the model's behalf. Fixed uid
# so a bind-mounted workspace has predictable ownership on the host.
RUN useradd --create-home --uid 10001 cagentic

WORKDIR /src
COPY . /src
RUN pip install --no-cache-dir . \
    && rm -rf /src /root/.cache

# Created before the USER switch so both are owned by it: a bind mount that
# the host provides empty inherits these, and the gateway must be able to write
# its config, sessions and SQLite state.
RUN mkdir -p /workspace /home/cagentic/.config/cagentic \
    && chown -R cagentic:cagentic /workspace /home/cagentic

USER cagentic
WORKDIR /workspace

ENV HOME=/home/cagentic \
    XDG_CONFIG_HOME=/home/cagentic/.config \
    PYTHONUNBUFFERED=1 \
    CAGENTIC_GATEWAY_BIND=0.0.0.0 \
    OLLAMA_HOST=http://host.docker.internal:11434

# Config, sessions, notes, tasks and state.sqlite3 all live here. Without a
# volume every restart is a fresh install — new gateway token included, so
# paired phones stop working.
VOLUME ["/home/cagentic/.config/cagentic", "/workspace"]

EXPOSE 8700

# `/` is the one route that isn't token-gated (it's what hands the token to the
# browser), and it still requires a loopback Host — which this is. curl isn't
# installed on slim, and adding it for one healthcheck isn't worth the layer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8700/', timeout=4).read(1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "cagentic"]
CMD ["--serve", "--port", "8700", "-C", "/workspace"]
