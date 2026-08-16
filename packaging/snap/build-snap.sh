#!/bin/bash
# Build Nearby Cast snap using Docker (Ubuntu 22.04 environment)
docker run --rm -v "$(pwd)":/project -w /project ubuntu:22.04 bash -c "
  apt-get update -q
  apt-get install -y snapcraft
  snapcraft --use-lxd
"
