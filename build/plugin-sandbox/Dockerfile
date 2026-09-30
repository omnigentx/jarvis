ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /opt/requirements.txt
USER 65532:65532
WORKDIR /plugin
