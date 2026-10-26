# syntax=docker/dockerfile:1
# mcp-workplace — a small image of its own. The shared Dockerfile.mcp bakes in whisper and docling
# models (~1 GB) that this server never uses. Build context: the cuga-apps/ directory.
#   docker build -f mcp_servers/workplace/Dockerfile -t mcp-workplace .
FROM python:3.13-slim

WORKDIR /app
COPY mcp_servers/workplace/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/__init__.py apps/_ports.py ./apps/
COPY mcp_servers/__init__.py ./mcp_servers/__init__.py
COPY mcp_servers/_core/ ./mcp_servers/_core/
COPY mcp_servers/workplace/ ./mcp_servers/workplace/

ENV PYTHONUNBUFFERED=1
EXPOSE 29108
# Secrets (WORKPLACE_MCP_TOKEN, SLACK_BOT_TOKEN, BOX_*) come from the runtime environment only.
CMD ["python", "-m", "mcp_servers.workplace.server"]
