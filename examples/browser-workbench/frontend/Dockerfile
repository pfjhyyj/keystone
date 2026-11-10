FROM debian:trixie-slim AS viewer
RUN apt-get update && apt-get install -y --no-install-recommends novnc \
    && mkdir -p /viewer \
    && cp -aL /usr/share/novnc/. /viewer/ \
    && cp /usr/share/doc/novnc/copyright /viewer/COPYRIGHT

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html app.js style.css /usr/share/nginx/html/workbench/
COPY --from=viewer /viewer/ /usr/share/nginx/html/workbench/novnc/
