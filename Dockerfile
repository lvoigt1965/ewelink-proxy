FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ ./templates/

# Default config — mount your own config.yaml to override
COPY config.yaml ./config.yaml

# Data dir for state.json (round-robin persistence + history)
RUN mkdir -p /data
ENV STATE_PATH=/data/state.json

EXPOSE 8001

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
