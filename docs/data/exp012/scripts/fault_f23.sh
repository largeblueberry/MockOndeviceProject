#!/bin/bash
# EXP-012 고장 F2(받는 쪽 강제 종료) / F3(브로커 강제 종료). .10 client -> .22, 5 Hz x 60 s, 20 s 시점에 5 s 고장
S=linux@192.168.20.22; C=linux@192.168.20.10; R=commbench/fault
ENV='cd ~/bootcampProject; export PYTHONPATH=src; PY=~/venvs/commbench/bin/python'
ssh -o BatchMode=yes $S "mkdir -p ~/$R"; ssh -o BatchMode=yes $C "mkdir -p ~/$R"
run() {   # $1=F2|F3  $2=proto
  f=$1; proto=$2; tag=${f}_$proto
  case $proto in udp) srv="--bind 0.0.0.0:6000"; dst=192.168.20.22:6000;; http*) srv="--bind 0.0.0.0:6001"; dst=192.168.20.22:6001;; mqtt*) srv="--broker 127.0.0.1:1884"; dst=192.168.20.22:1884;; esac
  echo "=== $tag start $(date +%T)"
  ssh -o BatchMode=yes $S "$ENV; \$PY -m psafe.tools.commbench server --proto $proto $srv --log ~/$R/srv_$tag.csv --seconds 70 >/dev/null 2>&1 < /dev/null" &
  sleep 3
  ssh -o BatchMode=yes $C "$ENV; \$PY -m psafe.tools.commbench client --proto $proto --server $dst --rate 5 --seconds 60 --log ~/$R/cli_$tag.csv >/dev/null 2>&1 < /dev/null" &
  sleep 20
  if [ $f = F2 ]; then
    ssh -o BatchMode=yes $S 'pkill -9 -f "[c]ommbench server"'; echo "  killed server $(date +%T.%N | cut -c1-12)"
    sleep 5
    ssh -o BatchMode=yes $S "$ENV; \$PY -m psafe.tools.commbench server --proto $proto $srv --log ~/$R/srv_$tag.csv --seconds 45 >/dev/null 2>&1 < /dev/null" &
    echo "  restarted server $(date +%T.%N | cut -c1-12)"
  else
    ssh -o BatchMode=yes $S 'pkill -9 -f "[m]osquitto -c /home/linux/commbench"'; echo "  killed broker $(date +%T.%N | cut -c1-12)"
    sleep 5
    ssh -o BatchMode=yes $S 'setsid nohup /usr/sbin/mosquitto -c /home/linux/commbench/mosq.conf >> ~/commbench/mosq.log 2>&1 < /dev/null & sleep 0.5'
    echo "  restarted broker $(date +%T.%N | cut -c1-12)"
  fi
  wait
  ssh -o BatchMode=yes $C "cat ~/$R/cli_$tag.csv" | ssh -o BatchMode=yes $S "cat > ~/$R/cli_$tag.csv"
  ssh -o BatchMode=yes $S "$ENV; \$PY -m psafe.tools.commbench analyze ~/$R/cli_$tag.csv ~/$R/srv_$tag.csv --fault-s 5"
}
for p in udp http http-new mqtt0 mqtt1; do run F2 $p; done
for p in mqtt0 mqtt1; do run F3 $p; done
ssh -o BatchMode=yes $S 'pgrep -a mosquitto'
echo "=== done $(date +%T)"
