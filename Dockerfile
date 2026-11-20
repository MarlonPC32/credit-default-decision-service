FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS dependencies

RUN python -m venv /opt/venv
COPY requirements.txt requirements-docker.lock /tmp/
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && /opt/venv/bin/python -m pip install --no-cache-dir --only-binary=:all: \
    -r /tmp/requirements.txt -c /tmp/requirements-docker.lock \
    && /opt/venv/bin/python -m pip check

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DECISIONS_DB=/var/lib/credit-default/decisions.db
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
COPY --chmod=644 src/*.py ./src/
COPY --chmod=644 models/model.joblib models/metadata.json ./models/
RUN chmod 755 /app/src /app/models \
    && install -d -o 10001 -g 10001 /var/lib/credit-default

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
