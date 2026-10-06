FROM python:3.13-slim

COPY requirements-runner.txt /tmp/requirements-runner.txt
RUN --mount=type=secret,id=benchmark_ca,required=false \
    if [ -s /run/secrets/benchmark_ca ]; then export PIP_CERT=/run/secrets/benchmark_ca; fi; \
    pip install --no-cache-dir -r /tmp/requirements-runner.txt

WORKDIR /bench
ENTRYPOINT ["sleep", "infinity"]
