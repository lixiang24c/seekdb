FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS native-build
COPY debian-packages.lock /tmp/debian-packages.lock
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261109T000000Z bookworm main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261109T000000Z bookworm-updates main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261109T000000Z bookworm-security main' \
      > /etc/apt/sources.list && \
    apt-get -o Acquire::Retries=3 update && \
    xargs apt-get -o Acquire::Retries=3 install -y --no-install-recommends < /tmp/debian-packages.lock && \
    rm -rf /var/lib/apt/lists/*
COPY native/topk.c /tmp/topk.c
RUN gcc -std=c11 -O3 -fPIC -shared -ffp-contract=off -DMV_WITH_PYTHON -I/usr/local/include/python3.12 /tmp/topk.c -o /tmp/libminivector.so -lm

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
WORKDIR /app
COPY requirements.lock /app/requirements.lock
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python3 -m pip install --disable-pip-version-check --no-cache-dir \
    --only-binary=:all: --require-hashes --retries 3 -r /app/requirements.lock
COPY minivectordb /app/minivectordb
COPY --from=native-build /tmp/libminivector.so /app/minivectordb/libminivector.so
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MINIVECTOR_REQUIRE_NATIVE=1
ENTRYPOINT ["python3", "-m", "minivectordb.server", "--data", "/data"]
