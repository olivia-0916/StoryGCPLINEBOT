# 使用官方 Python 映像檔（精簡版）
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 複製需求檔案並安裝套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式碼與憑證（Firebase 憑證要和 app.py 放同一層）
COPY . .

# 使用 Gunicorn 執行 Flask 應用，符合 Cloud Run 要求的 port
CMD ["gunicorn", "-b", "0.0.0.0:8080", "app:app"]
