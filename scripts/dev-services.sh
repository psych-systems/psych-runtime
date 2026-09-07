#!/usr/bin/env bash
# Start the real databases the store contract suite runs against.
#
# DESIGN.md §22: store tests run against real PostgreSQL, real MySQL and real
# DynamoDB Local. The in-memory adapter never proves the store contract. CI uses
# GitHub Actions service containers; this script is the same thing for a machine
# with no container runtime.
#
# Usage: scripts/dev-services.sh [start|stop|status]
set -euo pipefail

PG_BIN=${PG_BIN:-/usr/lib/postgresql/16/bin}
PG_DATA=${PG_DATA:-/var/lib/postgresql/data}
DDB_HOME=${DDB_HOME:-/opt/dynamodb-local}

start_postgres() {
  if pg_isready -h 127.0.0.1 -p 5432 -q 2>/dev/null; then echo "postgres already up"; return; fi
  mkdir -p /var/run/postgresql && chown postgres:postgres /var/run/postgresql
  su postgres -c "$PG_BIN/pg_ctl -D $PG_DATA -o '-p 5432 -c listen_addresses=127.0.0.1' -l /var/lib/postgresql/pg.log start"
  for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -q && break; sleep 1; done
  psql -h 127.0.0.1 -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='psych_test'" | grep -q 1 \
    || psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE psych_test"
  echo "postgres up on 127.0.0.1:5432"
}

start_mysql() {
  if mariadb-admin -h 127.0.0.1 -P 3306 -u psych -ppsych ping >/dev/null 2>&1; then echo "mysql already up"; return; fi
  mkdir -p /var/run/mysqld && chown mysql:mysql /var/run/mysqld
  setsid mariadbd --user=mysql --datadir=/var/lib/mysql \
    --socket=/var/run/mysqld/mysqld.sock --bind-address=127.0.0.1 --port=3306 \
    >/var/log/mariadb.log 2>&1 < /dev/null &
  disown || true
  for _ in $(seq 1 40); do mariadb-admin -h 127.0.0.1 -u psych -ppsych ping >/dev/null 2>&1 && break; sleep 1; done
  echo "mysql up on 127.0.0.1:3306"
}

start_dynamodb() {
  if curl -s -o /dev/null http://127.0.0.1:8000/ 2>/dev/null; then echo "dynamodb already up"; return; fi
  setsid java -Djava.library.path="$DDB_HOME/DynamoDBLocal_lib" \
    -jar "$DDB_HOME/DynamoDBLocal.jar" -inMemory -port 8000 \
    >/var/log/ddb.log 2>&1 < /dev/null &
  disown || true
  for _ in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 1; done
  echo "dynamodb-local up on 127.0.0.1:8000"
}

status() {
  pg_isready -h 127.0.0.1 -p 5432 || true
  mariadb-admin -h 127.0.0.1 -u psych -ppsych ping 2>&1 | tail -1 || true
  curl -s -o /dev/null -w 'dynamodb-local http %{http_code}\n' http://127.0.0.1:8000/ || true
}

case "${1:-start}" in
  start) start_postgres; start_mysql; start_dynamodb; status ;;
  stop)
    su postgres -c "$PG_BIN/pg_ctl -D $PG_DATA stop" || true
    mariadb-admin -h 127.0.0.1 -u psych -ppsych shutdown || true
    pkill -f DynamoDBLocal.jar || true
    ;;
  status) status ;;
  *) echo "usage: $0 [start|stop|status]" >&2; exit 2 ;;
esac
