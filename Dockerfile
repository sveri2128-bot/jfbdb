# Dockerfile — works on Render, Railway, and Fly.io
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot file
# Rename to bot.py for the platform you're using:
#   Render  → copy render_bot.py  as bot.py
#   Railway → copy railway_bot.py as bot.py
#   Fly.io  → copy fly_bot.py     as bot.py
COPY bot.py .

CMD ["python", "bot.py"]
