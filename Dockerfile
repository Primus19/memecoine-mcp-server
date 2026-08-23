FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir websockets>=13.0,<14.0
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir coinbase-advanced-py==1.8.4
RUN python -c "from coinbase.rest import RESTClient; print('Coinbase REST SDK import verified')"
COPY app ./app
EXPOSE 8080
CMD ["python", "-m", "app.server"]

