FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

COPY codex_claude_usage ./codex_claude_usage
COPY proxy.py ./proxy.py
# The MIT notice travels with the software. The licence requires it in "all
# copies or substantial portions", and this image is one: `.dockerignore`
# already re-admitted LICENSE, but nothing copied it in.
COPY LICENSE ./LICENSE
COPY vendor ./vendor
COPY web ./web

RUN groupadd --gid 10001 codexclaudeusage \
    && useradd --uid 10001 --gid codexclaudeusage --create-home \
        --home-dir /home/codexclaudeusage --shell /usr/sbin/nologin codexclaudeusage \
    && chmod 0755 /home/codexclaudeusage \
    && install -d -o codexclaudeusage -g codexclaudeusage -m 0755 /home/codexclaudeusage/.claude/projects \
    && install -d -o codexclaudeusage -g codexclaudeusage -m 0700 /data

ENV HOST=127.0.0.1
ENV PORT=8080
ENV CODEX_CLAUDE_USAGE_DB=/data/usage.db
ENV CODEX_CLAUDE_USAGE_THRESHOLDS=/data/limit-thresholds.json
ENV HOME=/home/codexclaudeusage
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import json,os,urllib.request; from codex_claude_usage.scanner import VERSION; port=os.environ['PORT']; d=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz',timeout=2)); assert d == {'service':'codex-claude-usage','status':'ok','version':VERSION}"]

USER codexclaudeusage

CMD ["python3", "-m", "codex_claude_usage.cli", "dashboard", "--no-browser"]
