# coordinated-workers
![PyPI](https://img.shields.io/pypi/v/coordinated-workers)

This library provides abstractions to simplify the creation and management of charms following the coordinator-worker pattern, like [Tempo](https://github.com/canonical/tempo-coordinator-k8s-operator), [Loki](https://github.com/canonical/loki-coordinator-k8s-operator), and [Mimir](https://github.com/canonical/mimir-coordinator-k8s-operator) charms:

- [`Coordinator`](src/coordinated_workers/coordinator.py): A class that takes care of the shared tasks of a coordinator.
- [`Worker`](src/coordinated_workers/worker.py): A class that handles the shared tasks of a worker.
- [`NginxConfig`](src/coordinated_workers/nginx.py): An Nginx configuration generator.

# How to release
 
Go to https://github.com/canonical/cos-coordinated-workers/releases and click on 'Draft a new release'.

Select a tag from the dropdown, or create a new one from the `main` target branch.

Enter a meaningful release title and in the description, put an itemized changelog listing new features and bugfixes, and whatever is good to mention.

Click on 'Publish release'.