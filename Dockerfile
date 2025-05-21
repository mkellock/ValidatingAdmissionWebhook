FROM python:3.9-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

# Expose webhook port
EXPOSE 8443

# Use Gunicorn for production readiness
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8443", "main:app"]