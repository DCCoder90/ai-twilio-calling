# Muse's Twilio outbound call service.
# Stdlib-only Python: no pip installs, nothing to pin.
FROM python:3.12-slim

WORKDIR /app
COPY server.py responder.py briefs.py transcripts.py ./

# Briefs (input) and transcripts (output) live on a mounted volume so the
# code image stays stateless.
RUN mkdir -p /data/briefs /data/transcripts \
    && useradd --system --no-create-home app \
    && chown -R app:app /data
VOLUME ["/data"]
USER app

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)" || exit 1

# --host 0.0.0.0: a pod/container must accept traffic on its own IP, not
# just loopback (probes, Service and ingress all arrive non-loopback).
# Set TWILIO_AUTH_TOKEN via a Secret in real deployments; without it the
# server accepts unsigned webhook requests (local dev mode) and logs a
# warning per request.
CMD ["python3", "server.py", "--host", "0.0.0.0", "--port", "8765", "--briefs-dir", "/data/briefs", "--transcripts-dir", "/data/transcripts"]
