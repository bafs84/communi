FROM dxflrs/garage:v2.2.0 AS upstream

FROM alpine:3.22
COPY --from=upstream /garage /garage
COPY garage.toml /etc/garage.toml
COPY garage-entrypoint.sh /usr/local/bin/garage-entrypoint.sh
COPY garage-healthcheck.sh /garage-healthcheck.sh
RUN chmod 755 /usr/local/bin/garage-entrypoint.sh /garage-healthcheck.sh
ENTRYPOINT ["/usr/local/bin/garage-entrypoint.sh"]
