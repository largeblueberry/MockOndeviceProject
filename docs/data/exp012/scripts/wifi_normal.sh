#!/bin/bash
# EXP-012 평상시: .10(client) -> .22(server), 5 Hz x 300 s, 방식별
S=linux@192.168.20.22; C=linux@192.168.20.10; R=commbench/wifi_normal
ENV='cd ~/bootcampProject; export PYTHONPATH=src; PY=~/venvs/commbench/bin/python'
ssh -o BatchMode=yes $S "mkdir -p ~/$R"; ssh -o BatchMode=yes $C "mkdir -p ~/$R"
for proto in udp http http-new mqtt0 mqtt1; do
  case $proto in udp) srv="--bind 0.0.0.0:6000"; dst=192.168.20.22:6000;; http*) srv="--bind 0.0.0.0:6001"; dst=192.168.20.22:6001;; mqtt*) srv="--broker 127.0.0.1:1884"; dst=192.168.20.22:1884;; esac
  echo "=== $proto start $(date +%T)"
  ssh -o BatchMode=yes $S "$ENV; \$PY -m psafe.tools.commbench server --proto $proto $srv --log ~/$R/srv_$proto.csv --seconds 310 >/dev/null 2>&1 < /dev/null" &
  sleep 3
  ssh -o BatchMode=yes $C "$ENV; \$PY -m psafe.tools.commbench client --proto $proto --server $dst --rate 5 --seconds 300 --log ~/$R/cli_$proto.csv >/dev/null 2>&1 < /dev/null" 2>&1
  wait
  ssh -o BatchMode=yes $C "cat ~/$R/cli_$proto.csv" | ssh -o BatchMode=yes $S "cat > ~/$R/cli_$proto.csv"
  ssh -o BatchMode=yes $S "$ENV; \$PY -m psafe.tools.commbench analyze ~/$R/cli_$proto.csv ~/$R/srv_$proto.csv"
done
echo "=== done $(date +%T)"
