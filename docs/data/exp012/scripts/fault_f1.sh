#!/bin/bash
# EXP-012 고장 F1 (네트워크 단절, 2026-11-26): .10 client -> .22, Wi-Fi, 5 Hz x 60 s, 20 s 시점에 5 s 단절
# 단절 = .22에서 nft로 .10과 주고받는 패킷을 양방향 모두 조용히 버린다 (Wi-Fi 연결 자체는 유지 -> 재접속 시간이 섞이지 않는다)
# 노트북 <-> 두 Pi ssh는 막히지 않는다. 전제: .22에 /etc/sudoers.d/commbench-nft (nft만 비밀번호 없이)
S=linux@192.168.20.22; C=linux@192.168.20.10; R=commbench/fault
ENV='cd ~/bootcampProject; export PYTHONPATH=src; PY=~/venvs/commbench/bin/python; CB=~/cb/commbench.py'
CUT='sudo -n /usr/sbin/nft -f - <<EOF
table inet cbf1 {
  chain in  { type filter hook input  priority -10; ip saddr 192.168.20.10 drop; }
  chain out { type filter hook output priority -10; ip daddr 192.168.20.10 drop; }
}
EOF'
HEAL='sudo -n /usr/sbin/nft delete table inet cbf1 2>/dev/null; true'
trap 'ssh -o BatchMode=yes $S "$HEAL"' EXIT                     # 중간에 멈춰도 규칙이 남지 않게
ssh -o BatchMode=yes $S "$HEAL; mkdir -p ~/$R"; ssh -o BatchMode=yes $C "mkdir -p ~/$R"
# 브로커 확인은 ss(포트)로 한다. pgrep -f는 이 ssh 명령줄 자체에 걸려 '이미 떠 있음'으로 잘못 판단했다 (2026-11-26)
ssh -o BatchMode=yes $S 'ss -ltn | grep -q ":1884 " || { setsid nohup /usr/sbin/mosquitto -c /home/linux/commbench/mosq.conf >> ~/commbench/mosq.log 2>&1 < /dev/null & sleep 1; }'

run() {   # $1=proto
  proto=$1; tag=F1_$proto
  case $proto in udp) srv="--bind 0.0.0.0:6000"; dst=192.168.20.22:6000;; tcp) srv="--bind 0.0.0.0:6002"; dst=192.168.20.22:6002;;
                 http*) srv="--bind 0.0.0.0:6001"; dst=192.168.20.22:6001;; mqtt*) srv="--broker 127.0.0.1:1884"; dst=192.168.20.22:1884;; esac
  ssh -o BatchMode=yes $S "rm -f ~/$R/*_$tag.csv"; ssh -o BatchMode=yes $C "rm -f ~/$R/*_$tag.csv"
  echo "=== $tag start $(date +%T)"
  ssh -o BatchMode=yes $S "$ENV; \$PY \$CB server --proto $proto $srv --log ~/$R/srv_$tag.csv --seconds 75 >/dev/null 2>&1 < /dev/null" &
  sleep 3
  ssh -o BatchMode=yes $C "$ENV; \$PY \$CB client --proto $proto --server $dst --rate 5 --seconds 60 --log ~/$R/cli_$tag.csv >/dev/null 2>&1 < /dev/null" &
  sleep 20
  ssh -o BatchMode=yes $S "$CUT"; echo "  cut  $(date +%T.%N | cut -c1-12)"
  sleep 5
  ssh -o BatchMode=yes $S "$HEAL"; echo "  heal $(date +%T.%N | cut -c1-12)"
  wait
  ssh -o BatchMode=yes $C "cat ~/$R/cli_$tag.csv" | ssh -o BatchMode=yes $S "cat > ~/$R/cli_$tag.csv"
  ssh -o BatchMode=yes $S "$ENV; \$PY \$CB analyze ~/$R/cli_$tag.csv ~/$R/srv_$tag.csv --fault-s 5"
}
for p in ${PROTOS:-udp tcp http http-new mqtt0 mqtt1}; do run $p; done
echo "=== done $(date +%T)"
