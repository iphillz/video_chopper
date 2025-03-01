FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Create directory for storing processed videos
RUN mkdir -p /app/videos

# Expose port for Flask application
EXPOSE 3000

# Run the application with Gunicorn with increased timeout
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--timeout", "300", "--log-level", "info", "app:app"] 