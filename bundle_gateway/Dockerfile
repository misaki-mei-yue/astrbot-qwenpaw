FROM python:3.12.14-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.14.4
COPY bundle_gateway /app/bundle_gateway
USER 65534:65534
CMD ["python", "-m", "bundle_gateway.server"]
