# Gunakan image Python yang lebih lengkap (misalnya, Debian Bullseye)
FROM python:3.11-slim-bullseye

# Set direktori kerja di container
WORKDIR /app

# Salin requirements.txt dan instal semua dependensi
# Ini harus dijalankan sebelum menyalin kode untuk memanfaatkan layer cache Docker
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode aplikasi Anda (termasuk webhook_server.py)
COPY . /app

# Perintah untuk menjalankan aplikasi (sesuai dengan Procfile sebelumnya)
# Railway akan menggunakan ini secara default jika Procfile tidak didefinisikan secara eksplisit
# BENAR: Menggunakan format shell untuk memastikan $PORT diinterpretasikan
CMD uvicorn webhook_server:app --host 0.0.0.0 --port 8080
