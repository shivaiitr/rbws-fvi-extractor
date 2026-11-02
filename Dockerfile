FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    libgl1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src ./src
COPY configs ./configs
COPY app.py ./
COPY assets ./assets
COPY .streamlit ./.streamlit

RUN pip install --no-cache-dir -r requirements.txt pytesseract

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_FILE_WATCHER_TYPE=none
# Reduce glibc allocator RSS spikes (common OOM cause on small Railway containers).
ENV MALLOC_ARENA_MAX=2

EXPOSE 8501

# Bind Railway's dynamic PORT; disable file watcher in containers (avoids restart loops).
CMD ["sh", "-c", "streamlit run app.py --server.address 0.0.0.0 --server.port ${PORT:-8501} --server.fileWatcherType none --server.headless true"]
