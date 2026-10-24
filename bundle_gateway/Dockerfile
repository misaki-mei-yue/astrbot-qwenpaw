FROM python:3.12.14-slim
WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.14.4 cryptography==50.0.2
COPY bundle_gateway /app/bundle_gateway
COPY bundle_updater/__init__.py bundle_updater/common.py /app/bundle_updater/
COPY bundle_updater/backup_archive.py /app/bundle_updater/
COPY bundle_updater/webdav_protocol.py /app/bundle_updater/
COPY bundle-version.json /app/bundle-version.json
USER 65534:65534
CMD ["python", "-m", "bundle_gateway.server"]
