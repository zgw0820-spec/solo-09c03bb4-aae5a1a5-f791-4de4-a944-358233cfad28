FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

EXPOSE 8080
# Plain HTTP on purpose: the Compose network and the demo registry are local.
# In a real cluster the service must be fronted by TLS (see README "未完成").
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
