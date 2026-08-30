# ERP Data Transformation API - production container image.
#
# WHY A CONTAINER AT ALL
# ----------------------
# App Service's built-in Python runtime cannot install OS packages, and this
# application's document pipeline shells out to Tesseract. Deploying to the
# built-in runtime would silently lose OCR - a capability the test suite and
# the Phase 3/6 evaluations both exercise - so the image installs it explicitly.
#
# The Python version is pinned to 3.13 to match the environment the project was
# developed and measured on, rather than "whatever the platform offers".

FROM python:3.13-slim AS runtime

# tesseract-ocr        : the OCR engine resolve_tesseract_command() finds on PATH
# tesseract-ocr-eng    : the English language data; tesseract cannot OCR without it
# libgl1, libglib2.0-0 : shared objects Pillow and PyMuPDF load at import time
# unixodbc             : only the SQL Server connector needs it, and that import
#                        is lazy - installed so an operator who configures a SQL
#                        Server source gets a working driver rather than a
#                        surprise ImportError at request time
# curl                 : used by the container health check below
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        unixodbc \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source, so a code change does not re-download torch.
# requirements.txt is the project's declared dependency set; pyproject.toml
# carries an empty `dependencies` list, so this file is the source of truth.
COPY requirements.txt ./

# CPU-only torch. The default index would pull the CUDA build - several GB of
# GPU libraries that a B1 App Service instance has no device for.
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --no-deps -e .

# Bake the embedding model into the image.
#
# Without this the first request reaches out to huggingface.co to download
# all-MiniLM-L6-v2, which makes a cold start depend on an external service and
# on outbound network policy. Baking it costs ~90 MB and makes startup
# self-contained. The model id is the one the application already defaults to.
ENV HF_HOME=/opt/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=0
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); print('embedding model cached into the image')"

# Defaults that make the container correct on its own. Every one of these is
# overridable by an App Service setting; none of them is a secret.
#
# ERP_API_HOST=0.0.0.0 is the single change external hosting requires, and the
# application already exposes it as configuration - there is no Azure-specific
# code anywhere in src/.
ENV ERP_API_HOST=0.0.0.0 \
    ERP_API_PORT=8000 \
    ERP_API_UPLOAD_DIR=/mnt/erp-cold/uploads \
    ERP_COLD_ARCHIVE_DIR=/mnt/erp-cold/archive \
    ERP_QDRANT_MODE=cloud \
    ERP_QDRANT_HOT_COLLECTION=erp_vectors_hot \
    ERP_QDRANT_WARM_COLLECTION=erp_vectors_warm

# A writable fallback for the case where the Azure Files mount is absent, so the
# container starts and REPORTS the problem instead of crash-looping before it
# can log anything.
RUN mkdir -p /mnt/erp-cold/archive /mnt/erp-cold/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/v1/health/live || exit 1

# The documented startup path, unchanged from local use.
CMD ["python", "-m", "erp_pipeline.api"]
