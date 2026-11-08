#!/usr/bin/env bash
# Containment smoke test. Save the output: it is raw material for the demo video.
set -u
NET=pr-test-net
echo "== BEFORE =="; docker ps -a; docker network ls
docker network create --internal $NET >/dev/null
docker run --rm --name t1 --network $NET \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 128 --memory 256m --cpus 0.5 --user 10001 alpine sh -c '
  touch /x 2>&1; echo "rootfs write rc=$?";
  wget -T3 -qO- http://1.1.1.1 2>&1; echo "internet rc=$?";
  wget -T3 -qO- http://169.254.169.254 2>&1; echo "metadata rc=$?";
  echo "--- env ---"; env'
docker network rm $NET >/dev/null
echo "== AFTER =="; docker ps -a; docker network ls
