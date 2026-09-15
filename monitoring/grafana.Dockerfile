FROM grafana/grafana:12.2.0
COPY monitoring/grafana/provisioning /etc/grafana/provisioning
COPY monitoring/grafana/dashboards   /etc/grafana/dashboards
