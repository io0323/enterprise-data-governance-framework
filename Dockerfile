FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY edgf ./edgf
COPY policies ./policies
RUN pip install --no-cache-dir ".[postgres]"
RUN useradd -r -u 10001 edgf
USER edgf
EXPOSE 8000
CMD ["uvicorn", "edgf.main:app", "--host", "0.0.0.0", "--port", "8000"]
