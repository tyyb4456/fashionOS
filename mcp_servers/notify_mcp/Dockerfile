FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install \
    --default-timeout=300 \
    -r requirements.txt
COPY server.py .
EXPOSE 8005
CMD ["python", "server.py"]