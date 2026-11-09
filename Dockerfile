FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS native-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc=4:12.2.0-3 gcc-12=12.2.0-14+deb12u1 libc6-dev=2.36-9+deb12u14 && rm -rf /var/lib/apt/lists/*
COPY native/topk.c /tmp/topk.c
RUN gcc -std=c11 -O3 -fPIC -shared -ffp-contract=off -DMV_WITH_PYTHON -I/usr/local/include/python3.12 /tmp/topk.c -o /tmp/libminivector.so

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY minivectordb /app/minivectordb
COPY --from=native-build /tmp/libminivector.so /app/minivectordb/libminivector.so
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MINIVECTOR_REQUIRE_NATIVE=1
ENTRYPOINT ["python3", "-m", "minivectordb.server", "--data", "/data"]
