FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV AGENTSYNC_DB=/data/cloud.db
EXPOSE 8765
CMD ["agentsync", "cloud"]
