# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY feishu_fastapi_sdk/ ./feishu_fastapi_sdk/
RUN pip install --no-cache-dir -r requirements.txt

COPY feishu_agent/ ./feishu_agent/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "feishu_agent.agent_main:app", "--host", "0.0.0.0", "--port", "8000"]
