FROM alpine:3.20

RUN apk add --no-cache tinyproxy \
    && mkdir -p /var/run/tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf

EXPOSE 8888

# tinyproxy logs to a real file (see tinyproxy.conf); tail it to stdout so
# `docker compose logs` still shows what the proxy is doing.
CMD sh -c 'touch /var/log/tinyproxy.log && tinyproxy -c /etc/tinyproxy/tinyproxy.conf && tail -F /var/log/tinyproxy.log'
