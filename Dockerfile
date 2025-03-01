FROM python:3.10-slim

# Create a non-root user to run Chrome
RUN groupadd -r chrome && useradd -r -g chrome -G audio,video chrome \
    && mkdir -p /home/chrome && chown -R chrome:chrome /home/chrome

# Install necessary packages including Chrome dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    wget \
    curl \
    gnupg \
    grep \
    unzip \
    xvfb \
    # Chrome dependencies
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    fonts-liberation \
    xdg-utils \
    # Cleanup
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome for headless operation
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && echo "Chrome installed: $(google-chrome --version)"

# Set up the Python environment
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir yt-dlp==2023.11.16 youtube-dl==2021.12.17

# Create directories for data and authentication
RUN mkdir -p /app/videos \
    && mkdir -p /app/auth \
    && mkdir -p /app/logs \
    && chown -R chrome:chrome /app/videos /app/auth /app/logs

# Copy YouTube cookie files into container (if available)
COPY cookies.txt /app/cookies.txt
COPY youtube_cookies.txt /app/youtube_cookies.txt
COPY auth/cookies.txt /app/auth/cookies.txt

# Set permissions for cookie files
RUN chmod 644 /app/cookies.txt /app/youtube_cookies.txt /app/auth/cookies.txt || true

# Copy application code
COPY . /app/

# Create startup script with Xvfb
RUN echo '#!/bin/bash\n\
# Start Xvfb\n\
Xvfb :99 -screen 0 1280x1024x24 -ac &\n\
# Wait for Xvfb to start\n\
sleep 1\n\
# Check if Chrome works\n\
DISPLAY=:99 google-chrome --version\n\
# Check Xvfb is running\n\
ps aux | grep Xvfb\n\
# Run the application\n\
DISPLAY=:99 gunicorn --bind 0.0.0.0:3000 app:app --log-level debug\n' > /app/start.sh \
    && chmod +x /app/start.sh

# Make files accessible to the chrome user
RUN chown -R chrome:chrome /app

# Switch to non-root user
USER chrome

# Expose port
EXPOSE 3000

# Set environment variables
ENV YDL_VERBOSE_DEBUG=true
ENV HTTP_PROXY=""
ENV HTTPS_PROXY=""
ENV DISPLAY=:99
ENV CHROME_BIN=/usr/bin/google-chrome

# Run start script
CMD ["/app/start.sh"] 