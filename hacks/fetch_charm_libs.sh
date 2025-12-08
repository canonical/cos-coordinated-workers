#!/bin/bash

if [ -n "$1" ]; then
  cd "$1" || { echo "Failed to cd into $1"; exit 1; }
fi

# fetch all charm libs required by the coordinated_workers package
charmcraft fetch-lib charms.data_platform_libs.v0.s3
charmcraft fetch-lib charms.grafana_k8s.v0.grafana_dashboard
charmcraft fetch-lib charms.prometheus_k8s.v0.prometheus_scrape
charmcraft fetch-lib charms.loki_k8s.v1.loki_push_api
charmcraft fetch-lib charms.tempo_coordinator_k8s.v0.tracing
charmcraft fetch-lib charms.observability_libs.v0.kubernetes_compute_resources_patch
charmcraft fetch-lib charms.catalogue_k8s.v1.catalogue
charmcraft fetch-lib charms.istio-beacon-k8s.v0.service_mesh