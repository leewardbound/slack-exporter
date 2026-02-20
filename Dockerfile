FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency management
RUN pip install uv

# Copy project files
COPY pyproject.toml .
COPY src/ src/
COPY scripts/ scripts/

# Install dependencies
RUN uv sync --no-dev

# Create data directories
RUN mkdir -p data attachments

# Run the daemon
CMD ["uv", "run", "python", "scripts/daemon.py"]
