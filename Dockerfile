FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY splunkpilot ./splunkpilot
RUN pip install .
RUN useradd -m app && mkdir -p /data && chown app /data
USER app
ENV SPLUNKPILOT_DB=/data/splunkpilot.db
EXPOSE 8000
ENTRYPOINT ["splunkpilot"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
