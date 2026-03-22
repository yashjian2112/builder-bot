FROM python:3.11-slim

WORKDIR /app

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
