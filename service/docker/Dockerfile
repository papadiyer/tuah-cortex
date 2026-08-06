# Jebat-Cortex runtime image (optional secondary target; launchd is primary).
#
# No third-party Python dependencies: the HTTP API is stdlib http.server and the
# MCP server is stdlib JSON-RPC, so there is no requirements install step.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# ripgrep is optional: the graph store degrades gracefully without it, but the
# fallback is more useful with it present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY core/    /app/core/
COPY api/     /app/api/
COPY workers/ /app/workers/
COPY mcp/     /app/mcp/
COPY cli/     /app/cli/
COPY config/  /app/config/
COPY run_cortex.sh /app/

RUN mkdir -p /app/data /app/logs

EXPOSE 8765

CMD ["python3", "-m", "cli.cortex_cli", "serve", "--host", "127.0.0.1", "--port", "8765"]
