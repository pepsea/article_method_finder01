FROM python:3.11-slim
WORKDIR /srv
# 依存を先に入れてキャッシュを効かせる
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
