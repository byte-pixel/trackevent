# Use Python 3.12 slim image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libc6 \
    libgcc-s1 \
    libstdc++6 \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (only Chromium for memory efficiency)
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Create output directory
RUN mkdir -p out/browser_profile

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Set display for headless operation (if needed by bundled CLI)
ENV DISPLAY=:99

# Run the Slack bot
CMD ["python", "slack_bot.py"]
