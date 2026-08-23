FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \\
    && pip install --no-cache-dir --no-deps coinbase-advanced-py==1.8.4 \\
    && python -c "from coinbase.rest import RESTClient; print('Coinbase REST SDK import verified')"
COPY app ./app
EXPOSE 8080
CMD ["python", "-m", "app.server"]

