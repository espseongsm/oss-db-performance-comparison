FROM python:3.13-slim

COPY requirements-runner.txt /tmp/requirements-runner.txt
RUN pip install --no-cache-dir -r /tmp/requirements-runner.txt

WORKDIR /bench
ENTRYPOINT ["sleep", "infinity"]
