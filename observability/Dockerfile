FROM prom/prometheus:v3.5.1@sha256:38c3b05c3bc744ff1b0b7b4eb82196026442845e62a1e2073795565da506d7a2 AS prometheus
COPY --chmod=644 prometheus.yml /etc/prometheus/prometheus.yml

FROM grafana/grafana:12.4.2@sha256:83749231c3835e390a3144e5e940203e42b9589761f20ef3169c716e734ad505 AS grafana
COPY --chmod=644 grafana/provisioning/datasources/prometheus.yml /etc/grafana/provisioning/datasources/prometheus.yml
COPY --chmod=644 grafana/provisioning/dashboards/default.yml /etc/grafana/provisioning/dashboards/default.yml
COPY --chmod=644 grafana/dashboards/credit-service.json /var/lib/grafana/dashboards/credit-service.json
USER root
RUN chmod 755 /var/lib/grafana/dashboards
USER 472
