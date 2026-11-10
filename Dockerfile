FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --home /app netip

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY netip_bridge ./netip_bridge
COPY tests ./tests

USER netip
VOLUME ["/config"]
# 34567+ = TCP de cada câmera virtual; 34569/udp = busca automática das NVRs
EXPOSE 34567-34580/tcp 34569/udp

ENTRYPOINT ["python", "-m", "netip_bridge"]
CMD ["run", "--config", "/config/config.yaml"]
