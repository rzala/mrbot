FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY bot/ bot/

# If your GitLab instance uses a self-signed or internal CA certificate,
# uncomment the lines below and place your CA cert file in the project root.
# COPY my_ca.crt /usr/local/share/ca-certificates/my_ca.crt
# RUN update-ca-certificates
# ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

RUN mkdir -p /data

CMD ["python", "-m", "bot.app"]
