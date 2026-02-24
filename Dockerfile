FROM python:3.11-slim

# System deps for Playwright + scipy
RUN apt-get update && apt-get install -y \
    wget curl gcc g++ \
    libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .
EXPOSE 8000
CMD ["python", "main.py"]
