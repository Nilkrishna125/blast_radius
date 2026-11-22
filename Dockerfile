FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The few packages sqlglot and its non-DuckDB tests import, at the labeling environment's
# versions (requirements.lock), so decision-time test runs match the labeling run.
RUN pip install --no-cache-dir pytz==2026.6 python-dateutil==4.0.0 tzdata==2026.6 \
    mypy_extensions==1.2.0 typing_extensions==4.16.0 numpy==2.5.5 pandas==3.1.1
COPY selector.py verify.py state.py /app/
COPY map /app/map
COPY base /app/base
# Import-state snapshot of the pinned sqlglot, taken in this exact image (two hash seeds, so
# values that differ between runs of the same code are known and ignored).
RUN cd /tmp && PYTHONHASHSEED=1 python /app/state.py /app/base /tmp/s1.json \
 && PYTHONHASHSEED=2 python /app/state.py /app/base /tmp/s2.json \
 && python /app/state.py --merge /tmp/s1.json /tmp/s2.json /app/map/state.json /app/base \
 && rm /tmp/s1.json /tmp/s2.json && chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
