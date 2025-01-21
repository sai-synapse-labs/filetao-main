ARG BASE_IMAGE=python:3.11-slim
FROM $BASE_IMAGE AS builder

# Set a non-interactive frontend to avoid any interactive prompts during the build
ARG DEBIAN_FRONTEND=noninteractive

# Create directory to copy files to
RUN mkdir -p /source/ /opt/tensorstorage/
WORKDIR /source

# Install dependencies first, so source code changes don't invalidate the build cache
COPY requirements.txt /source/
RUN --mount=type=cache,target=/root/.cache/ \
 python -m pip install --prefix=/opt/tensorstorage -r requirements.txt

COPY ./README.md ./setup.py ./requirements-dev.txt /source/
COPY ./neurons /source/neurons
COPY ./storage /source/storage
RUN python -m pip install --prefix=/opt/tensorstorage --no-deps .

# symlink lib/pythonVERSION to lib/python so path doesn't need to be hardcoded
RUN ln -rs /opt/tensorstorage/lib/python* /opt/tensorstorage/lib/python
COPY ./bin /opt/tensorstorage/bin
COPY ./scripts /opt/tensorstorage/scripts

FROM $BASE_IMAGE AS tensorstorage

RUN mkdir -p ~/.bittensor/wallets && \
    mkdir -p /etc/redis/

COPY --from=builder /opt/tensorstorage /opt/tensorstorage

ENV PATH="/opt/tensorstorage/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/tensorstorage/lib:${LD_LIBRARY_PATH}"
ENV REBALANCE_SCRIPT_PATH=/opt/tensorstorage/scripts/rebalance_deregistration.sh
ENV PYTHONPATH="/opt/tensorstorage/lib/python/site-packages/:${PYTHONPATH}"

ENTRYPOINT ["/opt/tensorstorage/scripts/docker/entrypoint.sh"]
