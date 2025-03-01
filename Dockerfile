FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    curl \
    wget \
    grep \
    && rm -rf /var/lib/apt/lists/*

# Update pip and install yt-dlp separately to ensure the latest version
RUN pip install --upgrade pip && \
    pip install --no-cache-dir yt-dlp==2023.11.16 youtube-dl==2023.7.9

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Copy YouTube cookie files
COPY cookies.txt /app/cookies.txt
COPY youtube_cookies.txt /app/youtube_cookies.txt

# Create directory for storing processed videos
RUN mkdir -p /app/videos

# Create a directory for YouTube cookies and copy cookies.txt there too
RUN mkdir -p /app/auth
COPY auth/cookies.txt /app/auth/cookies.txt

# Set proper permissions on cookie files
RUN chmod 644 /app/cookies.txt /app/youtube_cookies.txt /app/auth/cookies.txt

# Expose port for Flask application
EXPOSE 3000

# Set environment variable to show more debug info
ENV YDL_VERBOSE_DEBUG=1
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""

# Run the application with Gunicorn with increased timeout
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--timeout", "300", "--log-level", "debug", "app:app"] 