FROM python:3.12-slim

# System deps: git (gitpython needs it), bandit needs gcc
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Repos to scan are mounted at runtime under /repos
RUN mkdir -p /repos
