FROM python:3.11-slim

WORKDIR /app

# Install git (needed for GitHub repo cloning) and clean up
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create data directories
RUN mkdir -p data/mockups data/sessions

# Expose port (Railway sets PORT env var)
EXPOSE 8080

CMD ["python3", "main.py"]
