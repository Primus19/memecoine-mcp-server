# Use the AWS public mirror of the Docker Official Image. Railway's Docker Hub
# metadata requests can be rate-limited or intermittently unavailable.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir "coinbase-advanced-py==1.8.4" --no-deps
RUN python -c "from coinbase.rest import RESTClient; print('Coinbase REST SDK import verified')"
COPY app ./app
EXPOSE 8080
CMD ["python", "-m", "app.server"]
