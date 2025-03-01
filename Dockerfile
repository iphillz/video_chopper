FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    gnupg \
    grep \
    unzip \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for Selenium
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir 'yt-dlp==2023.11.16' \
    && pip install --no-cache-dir 'youtube-dl==2023.7.9'

# Create directories for videos and auth
RUN mkdir -p /app/videos /app/auth

# Copy YouTube cookie files to multiple locations for maximum compatibility
COPY cookies.txt /app/cookies.txt
COPY youtube_cookies.txt /app/youtube_cookies.txt
COPY auth/cookies.txt /app/auth/cookies.txt

# Set proper permissions for cookie files
RUN chmod 644 /app/cookies.txt /app/youtube_cookies.txt /app/auth/cookies.txt

# Copy application code
COPY . /app/

# Expose port for the Flask application
EXPOSE 3000

# Set environment variables for debugging
ENV YDL_VERBOSE_DEBUG=1
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV DISPLAY=:99

# Create a startup script that starts Xvfb and the application
RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1280x1024x16 &\nexec gunicorn --workers=2 --threads=2 --timeout=300 --bind=0.0.0.0:3000 app:app --log-level=debug' > /app/start.sh \
    && chmod +x /app/start.sh

# Run the application
CMD ["/app/start.sh"] 