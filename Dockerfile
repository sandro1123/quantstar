FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml .
COPY config/ config/
COPY src/ src/

# 非 root 运行 (uid/gid 10001); data/logs 卷需对该 uid 可写
RUN groupadd --system --gid 10001 quantstar \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin quantstar \
    && mkdir -p data logs \
    && chown -R quantstar:quantstar /app/data /app/logs

ENV PYTHONPATH=/app/src
ENV CONFIG_ENV=prod

USER quantstar

# F6.4: 容器入口对齐生产 API (quantstar-api.service 同款 gunicorn)。
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "web.api:app"]
