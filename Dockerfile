FROM node:20-alpine AS frontend-builder
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install
COPY frontend/ .
RUN npm run build

FROM python:3.12-alpine
WORKDIR /app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV TZ=UTC

# su-exec drops from root to the requested PUID/PGID in entrypoint.sh; tzdata
# provides the /usr/share/zoneinfo data TZ is symlinked from. Nothing else is
# installed: every package left in the image shows up in vulnerability scans.
RUN apk upgrade --no-cache && apk add --no-cache \
    su-exec \
    tzdata

COPY backend/requirements.txt .
# pip and setuptools are removed afterwards since nothing installs packages at
# runtime, and pip's own findings show up in scans.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools
COPY backend/ .
COPY --from=frontend-builder /app/dist ./static
RUN mkdir -p /app/data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
