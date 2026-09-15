FROM vnoj/vnoj-base:latest

# Exec form: compose `command` replaces CMD (worker vs beat).
# Shell-form `ENTRYPOINT celery ... worker` ignored extra args, and
# exec-form with `worker` baked in concatenated beat into a worker.
ENTRYPOINT ["celery", "-A", "dmoj_celery"]
CMD ["worker", "-l", "info", "--concurrency=2"]
