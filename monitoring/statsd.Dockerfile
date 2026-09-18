FROM prom/statsd-exporter:v0.28.0
COPY monitoring/statsd_mapping.yml /etc/statsd/mapping.yml
