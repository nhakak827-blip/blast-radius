FROM alpine/git@sha256:2306e2b05ee0ef99f5d25b0779f645b24ae9f91ba187107503592fcdfe78da3a AS upstream
RUN git init /sqlglot \
    && git -C /sqlglot fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
    && git -C /sqlglot checkout --detach FETCH_HEAD \
    && rm -rf /sqlglot/.git

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS labeling
COPY requirements.lock /build/requirements.lock
RUN pip install --no-cache-dir -r /build/requirements.lock
COPY --from=upstream /sqlglot/sqlglot /sqlglot/sqlglot
COPY --from=upstream /sqlglot/tests /sqlglot/tests
COPY --from=upstream /sqlglot/pyproject.toml /sqlglot/pyproject.toml
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN python -m compileall -q /sqlglot

FROM labeling AS index
RUN pip install --no-cache-dir coverage==7.17.4
COPY build_index.py inline_pool.py comparisons.py build_trace.py /build/
RUN python /build/build_index.py /sqlglot /build/impact.json.gz \
    && chmod 644 /build/impact.json.gz

FROM labeling
COPY --from=index /build/impact.json.gz /app/impact.json.gz
COPY selector.py probe.py inline_pool.py comparisons.py /app/
USER 65534:65534
ENV HOME=/tmp BLAST_RADIUS_CONTAINER=1
ENTRYPOINT ["python", "/app/selector.py"]
