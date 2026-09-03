# syntax=docker/dockerfile:1

# Sandbox image for running Claude Code with --dangerously-skip-permissions.
#
# The container is the security boundary. Nothing from the host filesystem is
# visible inside except the repository bind mount declared in
# compose.claude.yaml, so an agent running here cannot touch host files that
# live outside this codebase. Everything the agent needs to work on LifeAgent
# (node, uv, python 3.12, git, psql) is baked in, because the runtime user is
# unprivileged and cannot apt-get anything at run time.
#
# To add a tool: edit this file and rebuild (scripts/claude-sandbox.sh build).

FROM node:22.14.0-bookworm-slim

ARG CLAUDE_CODE_VERSION=latest
# Match the uv/python pair used by infra/Dockerfile so lockfile resolution
# behaves the same inside the sandbox as it does in the app image.
ARG PYTHON_VERSION=3.12.8

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        less \
        openssh-client \
        postgresql-client \
        procps \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# uv is pinned to the same version the application image installs.
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /usr/local/bin/

# Interpreters live under /opt, not under /home/node. /home/node is a named
# volume at run time and is only seeded from the image the first time it is
# created, so anything that must be refreshed by an image rebuild has to sit
# outside it.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_LINK_MODE=copy
RUN uv python install "${PYTHON_VERSION}" \
    && chmod -R a+rX /opt/uv

# Claude Code is installed as root into /usr/local so an image rebuild is the
# only way it changes; the autoupdater is disabled to match.
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CONFIG_DIR=/home/node/.claude \
    NPM_CONFIG_PREFIX=/home/node/.npm-global \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    UV_CACHE_DIR=/home/node/.cache/uv \
    PATH=/home/node/.npm-global/bin:/workspace/.venv/bin:$PATH

# Seed the home directory in the image. Docker copies this content — and its
# ownership — into the empty named volume on first mount, which is what lets
# the container run as an unprivileged user with every capability dropped.
RUN mkdir -p \
        /home/node/.claude \
        /home/node/.npm-global/bin \
        /home/node/.cache/uv \
        /home/node/.local/bin \
    && printf '%s\n' \
        '[safe]' \
        '    directory = /workspace' \
        '[init]' \
        '    defaultBranch = main' \
        > /home/node/.gitconfig \
    && printf '%s\n' \
        'export PS1="sandbox:\w\$ "' \
        'export HISTFILE=/home/node/.bash_history' \
        > /home/node/.bashrc \
    && chown -R node:node /home/node

COPY entrypoint.sh /usr/local/bin/claude-sandbox-entrypoint
RUN chmod 0755 /usr/local/bin/claude-sandbox-entrypoint

WORKDIR /workspace
USER node

ENTRYPOINT ["/usr/local/bin/claude-sandbox-entrypoint"]
CMD ["claude", "--dangerously-skip-permissions"]
