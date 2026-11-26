#!/bin/bash
# EXP-012 tcp 추가 (2026-11-26): .10 client -> .22 server, Wi-Fi
#  1) 평상시 5 Hz x 300 s   2) F2 받는 쪽 강제 종료 (5 Hz x 60 s, 20 s 시점에 5 s)
# 새 commbench.py는 아직 커밋 전이라 Pi의 ~/cb/commbench.py로 복사해서 돌린다 (Pi git 트리를 깨끗하게 둔다)
S=linux@192.168.20.22; C=linux@192.168.20.10
ENV='cd ~/bootcampProject; export PYTHONPATH=src; PY=~/venvs/commbench/bin/python; CB=~/cb/commbench.py'
SRV="--bind 0.0.0.0:6002"; DST=192.168.20.22:6002
ssh -o BatchMode=yes $S "mkdir -p ~/commbench/wifi_normal ~/commbench/fault"
ssh -o BatchMode=yes $C "mkdir -p ~/commbench/wifi_normal ~/commbench/fault"

R=commbench/wifi_normal
echo "=== normal tcp start $(date +%T)"
ssh -o BatchMode=yes $S "$ENV; \$PY \$CB server --proto tcp $SRV --log ~/$R/srv_tcp.csv --seconds 310 >/dev/null 2>&1 < /dev/null" &
sleep 3
ssh -o BatchMode=yes $C "$ENV; \$PY \$CB client --proto tcp --server $DST --rate 5 --seconds 300 --log ~/$R/cli_tcp.csv >/dev/null 2>&1 < /dev/null"
wait
ssh -o BatchMode=yes $C "cat ~/$R/cli_tcp.csv" | ssh -o BatchMode=yes $S "cat > ~/$R/cli_tcp.csv"
ssh -o BatchMode=yes $S "$ENV; \$PY \$CB analyze ~/$R/cli_tcp.csv ~/$R/srv_tcp.csv"

R=commbench/fault; tag=F2_tcp
echo "=== $tag start $(date +%T)"
ssh -o BatchMode=yes $S "$ENV; \$PY \$CB server --proto tcp $SRV --log ~/$R/srv_$tag.csv --seconds 70 >/dev/null 2>&1 < /dev/null" &
sleep 3
ssh -o BatchMode=yes $C "$ENV; \$PY \$CB client --proto tcp --server $DST --rate 5 --seconds 60 --log ~/$R/cli_$tag.csv >/dev/null 2>&1 < /dev/null" &
sleep 20
ssh -o BatchMode=yes $S 'pkill -9 -f "[c]ommbench.py server"'; echo "  killed server $(date +%T.%N | cut -c1-12)"
sleep 5
ssh -o BatchMode=yes $S "$ENV; \$PY \$CB server --proto tcp $SRV --log ~/$R/srv_$tag.csv --seconds 45 >/dev/null 2>&1 < /dev/null" &
echo "  restarted server $(date +%T.%N | cut -c1-12)"
wait
ssh -o BatchMode=yes $C "cat ~/$R/cli_$tag.csv" | ssh -o BatchMode=yes $S "cat > ~/$R/cli_$tag.csv"
ssh -o BatchMode=yes $S "$ENV; \$PY \$CB analyze ~/$R/cli_$tag.csv ~/$R/srv_$tag.csv --fault-s 5"
echo "=== done $(date +%T)"
