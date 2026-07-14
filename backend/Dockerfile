FROM python:3.12-slim

# GDAL comes bundled in the rasterio wheels, but the wheel still links a few
# system libs that the slim image lacks.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the WhiteboxTools native binary into the image so cold starts don't
# re-download it (falls back to pysheds at runtime if this ever fails).
RUN python -c "import whitebox; whitebox.WhiteboxTools()" || true

COPY app ./app
COPY start.sh ./start.sh
RUN chmod +x start.sh && mkdir -p data
ENV AWS_NO_SIGN_REQUEST=YES
EXPOSE 8000
# start.sh honors $PORT and can spawn an embedded RQ worker process
# (WORKER_EMBEDDED=1) for single-container hosts; compose overrides the
# command for the dedicated worker service.
CMD ["./start.sh"]
