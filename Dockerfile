FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code.
# service_account.json is NOT copied into the image: it is gitignored and
# .dockerignore-excluded to keep the secret out of the build context. At
# runtime it is provided via GOOGLE_APPLICATION_CREDENTIALS (env var) set
# to a Cloud Run secret mount / mounted volume. See README.md.
COPY src/ ./src/
COPY app.py .

EXPOSE 8501

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
