FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot
COPY bot.py .

# Create downloads folder
RUN mkdir -p downloads

EXPOSE 8000

# Run bot
CMD ["python", "-u", "bot.py"]
