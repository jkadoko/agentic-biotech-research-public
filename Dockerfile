FROM python:3.11-slim

# System deps for lxml, thefuzz speedup, and ChromaDB native libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure output dir exists (bind-mount target)
RUN mkdir -p /app/output

# Default: Streamlit UI (overridden by scheduler service in docker-compose.yml)
EXPOSE 8501
CMD ["streamlit", "run", "app/main.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
