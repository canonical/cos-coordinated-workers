
# Summary
See [this discourse post](https://discourse.charmhub.io/t/cos-lite-docs-managing-deployments-of-cos-lite-ha-addons/15213), specifically the first paragraphs. 
# Telemetry
## Metrics
* Metrics configuration is provided by the Coordinator to a metrics scraper via the `metrics-endpoint` relation endpoint on the `prometheus_scrape` interface.  The metrics configuration provided includes all workloads (the Coordinator's nginx and all Worker workloads)
* Coordinator:
	* uses `MetricsEndpointProvider` to send alert rules to the scraper
	* uses `MetricsEndpointProvider` to send scrape targets for all workloads in the coordinated-worker solution (nginx and all worker workloads)
	* nginx workload's metrics are scraped by the scraper
* Worker:
	* no metrics configuration occurs in the Worker charm
	* workload metrics are scraped by the scraper `GET` requests direct to every Worker unit

Relation topology:
<img width="1243" height="347" alt="image" src="https://github.com/user-attachments/assets/87f60e3a-7cfb-4c61-9e6c-9fe89876ef19" />

Data flow:
<img width="1200" height="514" alt="image" src="https://github.com/user-attachments/assets/22afa40c-e6d1-45a4-9be5-a50fd3ea4570" />

## Logs
* Logging configuration is provided to the Coordinator via the `logging` relation endpoint using the `loki_push_api` interface
* Coordinator:
	* uses `LogForwarder` to configure pebble to send all coordinator charm and workload (nginx) logs to the logging store
	* uses `LokiPushApiConsumer` to [idk?](https://github.com/canonical/cos-coordinated-workers/blob/783e1d303ab358f7f120498dc0d472412badb3c8/src/coordinated_workers/coordinator.py#L325-L329)
	* extracts logging configuration by interrogating the `logging` relation data directly (using `loki_endpoints_by_unit`, not by using a library from loki) and injects that into the `Cluster` relation
* Worker:
	* uses `ManualLogForwarder` (a local version similar to `LogForwarder`) to configure pebble to send all Worker charm and workload logs to the logging store, using configuration from the `Cluster` relation

Relation topology:
<img width="1463" height="659" alt="image" src="https://github.com/user-attachments/assets/8b3dd852-d659-426b-a8a1-ed2398d82ada" />

Data flow:
<img width="1463" height="835" alt="image" src="https://github.com/user-attachments/assets/cbcc1c98-b558-4b26-89de-84e5aa9d0dbe" />

## Traces
* Tracing configuration is provided to the Coordinator via the `charm-tracing` and `workload-tracing` relation endpoints, both using the `tracing` interface
	* `charm-tracing` in the Coordinator is used to configure tracing in **all** coordinated-worker charms (Coordinator and all related Workers)
* Coordinator:
	* gets tracing configuration from `charm-tracing` and `workload-tracing` by instantiating a `TracingEndpointRequirer` for each
	* sends the protocols required for `charm-tracing` and `workload-tracing` back to the related tracing provider using the instantiated `TracingEndpointRequirer` objects
	* uses the `charm-tracing` configuration to:
		* configure the Coordinator charm to emit traces using ops.tracing machinery
		* forward `charm-tracing` configuration to the Workers via the `Cluster` databag
	* uses the `workload-tracing` configuration to:
		* forward `workload-tracing` configuration to the Workers via the `Cluster` relation
* Worker:
	* gets tracing configuration from the `Cluster` relation
	* configures the workload to emit traces to the provided tracing store

Relation topology:
<img width="1541" height="780" alt="image" src="https://github.com/user-attachments/assets/1b490d1e-f2e0-4063-bcc0-c7404c567669" />

Data flow:
<img width="1541" height="1062" alt="image" src="https://github.com/user-attachments/assets/e48acf60-02c8-4704-bf04-2e74368b28a7" />
