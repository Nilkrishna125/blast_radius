FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py /app/selector.py
COPY map /app/map
COPY base /app/base
RUN chmod -R a+rX /app
CMD ["python", "/app/selector.py"]
