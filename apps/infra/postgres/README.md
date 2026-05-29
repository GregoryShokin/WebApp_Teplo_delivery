# PostgreSQL infrastructure notes

Dev uses the official `postgres:16` image from `apps/docker-compose.yml`.

Production is intentionally not defined here yet. The production plan should cover:

- backup and restore drills;
- encrypted secrets;
- TLS termination;
- firewall rules;
- migration workflow;
- monitoring and log retention;
- private artifact storage for `research/private/`.
