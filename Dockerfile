FROM gcc:12-bookworm@sha256:44482187aa3865ad1e2477dc8c56bae4fd8e071e7d99c5f44956effc7f7cc846 AS native-build
COPY native/ /tmp/native/
RUN gcc -std=c11 -O3 -fPIC -shared -ffp-contract=off -fno-fast-math /tmp/native/screen.c -o /tmp/libscreen.so -lm

FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
WORKDIR /app
COPY minidb /app/minidb
COPY --from=native-build /tmp/libscreen.so /app/minidb/libscreen.so
RUN mkdir -p /data && chmod 0777 /data
ENTRYPOINT ["python3", "-B", "-u", "-m", "minidb.server", "/data"]
