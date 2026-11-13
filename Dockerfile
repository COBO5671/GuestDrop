FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd -r -u 10001 -m appuser
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY templates ./templates
COPY static ./static
RUN mkdir -p /data /uploads && chown -R appuser:appuser /app /data /uploads
USER appuser
EXPOSE 8081
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "3600", "--bind", "0.0.0.0:8081", "app:app"]
