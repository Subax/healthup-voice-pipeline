#!/usr/bin/env bash
# 로컬 CA와 서버 인증서를 만든다. 예: scripts/gen_cert.sh 192.168.0.10 healthup.local
# 결과: certs/ca.pem(태블릿에 설치), certs/server.pem, certs/server.key
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -lt 1 ]; then
  echo "사용법: $0 <호스트이름 또는 IP>..." >&2
  exit 1
fi
D=certs
if [ -e "$D/ca.pem" ] || [ -e "$D/server.pem" ]; then
  echo "certs/에 인증서가 이미 있다. 덮어쓰지 않는다."
  echo "새로 만들려면 certs/를 지운 뒤 다시 실행한다."
  exit 0
fi
mkdir -p "$D"
chmod 700 "$D"
SAN="DNS:localhost,IP:127.0.0.1"
for h in "$@"; do
  if [[ "$h" =~ ^[0-9.]+$ ]] || [[ "$h" == *:* ]]; then SAN+=",IP:$h"; else SAN+=",DNS:$h"; fi
done
# CA (10년)
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=HealthUp Local CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$D/ca.key" -out "$D/ca.pem" 2>/dev/null
# 서버 인증서 (모바일 OS 제한 때문에 397일 이하)
openssl req -newkey rsa:2048 -nodes -subj "/CN=$1" -keyout "$D/server.key" -out "$D/server.csr" 2>/dev/null
printf "subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" "$SAN" > "$D/server.ext"
openssl x509 -req -in "$D/server.csr" -CA "$D/ca.pem" -CAkey "$D/ca.key" -CAcreateserial \
  -days 397 -extfile "$D/server.ext" -out "$D/server.pem" 2>/dev/null
rm -f "$D/server.csr" "$D/server.ext" "$D/ca.srl"
chmod 600 "$D"/*.key
echo "만들었다: $D/ca.pem, $D/server.pem, $D/server.key (SAN: $SAN)"
cat <<MSG

태블릿에 ca.pem 설치 (ca.pem을 태블릿으로 옮긴다. ca.key는 옮기지 않는다):
- Android: 설정 → 보안 → 암호화 및 사용자 인증정보 → 인증서 설치 → CA 인증서 → ca.pem 선택
- iOS: ca.pem을 열어 프로파일을 설치한다. 설정 → 일반 → 정보 → 인증서 신뢰 설정에서 HealthUp Local CA를 켠다
서버 인증서는 397일 뒤 만료된다. 그때 certs/를 지우고 다시 만든다.
MSG
