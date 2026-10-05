# ใช้กับ host ที่รับ Docker (เช่น Koyeb, Hugging Face Spaces, เซิร์ฟเวอร์ในโรงงาน)
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PORT=8780
EXPOSE 8780
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
