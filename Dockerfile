# ============================================================
# JD online — JAVDB 媒体管理中心（Flask + SQLite）
# 自托管个人工具。构建：
#   docker build -t desstg/jd-online:latest .
#
# 设计约定：
#   - 代码在 /app；运行数据（config.json / javdb.db）在 /data 卷，两者分离。
#   - 不把任何凭据、config.json、javdb.db 打进镜像，用户运行时用卷提供。
#   - 以非 root（appuser，uid 1000）运行。
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖层单独拷入并安装，利用 Docker 构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码（不含任何凭据 / 数据库）
COPY webapp.py main.py ./
COPY javdb ./javdb
COPY web ./web

# 非 root 用户；数据目录归属该用户，保证首次启动可写
RUN useradd -r -u 1000 -m appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /data && chown appuser:appuser /data

USER appuser

# 运行数据目录（config.json / javdb.db 都在这里，挂载卷持久化）
WORKDIR /data
VOLUME /data

EXPOSE 9091

CMD ["python", "/app/webapp.py"]
