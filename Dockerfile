FROM python:3.11-slim

# 面板在线监测台：零第三方依赖，只用 Python 标准库
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY panel_monitor.py /app/panel_monitor.py
COPY web /app/web

# 配置与历史数据都放 /data，运行时用 -v 挂到宿主机便于编辑和持久化
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8765

# 容器内必须监听 0.0.0.0，否则 Docker 端口映射 / frp 转发访问不到
CMD ["python3", "panel_monitor.py", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--no-browser", \
     "--config", "/data/servers.json"]
