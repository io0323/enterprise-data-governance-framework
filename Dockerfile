FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# 既定は本番相当: OIDC の JWT 検証。dev モードは docker-compose.dev.yml でのみ有効化する
ENV EDGF_ENV=production EDGF_AUTH_MODE=jwt
COPY pyproject.toml alembic.ini ./
COPY edgf ./edgf
COPY policies ./policies
RUN pip install --no-cache-dir ".[postgres]"
RUN useradd -r -u 10001 edgf
USER edgf
EXPOSE 8000
# スキーマは起動前に Alembic で最新化する (create_all は使わない)。
# アクセスログは edgf の構造化ログ (request_id 付き) が出力するため uvicorn 側は無効化する。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn edgf.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
