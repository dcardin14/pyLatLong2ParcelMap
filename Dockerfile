FROM python:3.11-slim

WORKDIR /app

# System dependencies for GIS libraries, ExifTool, and HTTPS
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    libexpat1 \
    libimage-exiftool-perl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY . .

# Configure Matplotlib for server-side rendering
ENV MPLBACKEND=Agg
ENV PYTHONUNBUFFERED=1

# Start the Flask application using Gunicorn
# CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 300 parcel_web:app"]
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 2 --timeout 300 --access-logfile - --error-logfile - --capture-output parcel_web:app"]