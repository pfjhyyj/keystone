FROM debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium chromium-sandbox \
       fonts-liberation fonts-noto-cjk openbox python3 tigervnc-standalone-server tini \
       websockify wmctrl x11-utils \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 browser \
    && useradd -u 10001 -g browser --create-home browser \
    && mkdir -p /data/profile
COPY runtime.py auth.py archive.py /opt/browser-runtime/
COPY entrypoint.sh /usr/local/bin/browser-runtime
RUN chmod 0755 /usr/local/bin/browser-runtime
ENTRYPOINT ["/usr/local/bin/browser-runtime"]
