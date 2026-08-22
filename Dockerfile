FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser
EXPOSE 8080
CMD ["python", "-m", "app.server"]

