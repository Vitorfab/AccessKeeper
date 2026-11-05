FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY accesskeeper ./accesskeeper
COPY wsgi.py reset_master.py ./

RUN useradd --system --no-create-home --uid 10001 appuser \
    && mkdir -p /app/instance \
    && chown appuser /app/instance
USER appuser

ENV HOST=0.0.0.0 PORT=5000
EXPOSE 5000
VOLUME ["/app/instance"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=3)"

CMD ["python", "wsgi.py"]
