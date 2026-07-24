# AAC boundary-seal A2A agent — container image for Cloud Run.
#
# Deploys as its OWN service; it does not touch the anchor's service. The only
# outbound dependency is the public transparency log (AAC_ANCHOR_URL), reached
# over HTTPS with no credentials.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY serve_boundary_agent.py .

# Cloud Run injects $PORT and terminates TLS; the app honours $PORT and renders
# the Agent Card from the forwarded scheme+host, so the advertised interface URL
# is correct without a second deploy to inject the assigned URL.
EXPOSE 8080
CMD ["sh", "-c", "exec python serve_boundary_agent.py --host 0.0.0.0 --port ${PORT}"]
