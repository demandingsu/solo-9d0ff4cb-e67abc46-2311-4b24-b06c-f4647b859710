FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷应用代码
COPY app ./app

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 2 个 worker、每 worker 4 线程；模块级 app 实例供 gunicorn 加载
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "app.web:app"]
