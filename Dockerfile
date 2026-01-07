# Use a lightweight Python version
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# 1. Install system tools (needed for some Python packages)
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

# 2. Copy the dependency file first (this speeds up rebuilds)
COPY pyproject.toml .

# 3. Install dependencies globally (Simplest way for Docker)
# This reads your pyproject.toml and installs fastapi, celery, etc.
RUN pip install --no-cache-dir .

# 4. Copy the rest of your code
COPY . .

# (Optional) Default command, though docker-compose overrides this
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]