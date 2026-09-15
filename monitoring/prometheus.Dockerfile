FROM prom/prometheus:v3.6.0
COPY monitoring/prometheus.yml /etc/prometheus/prometheus.yml
