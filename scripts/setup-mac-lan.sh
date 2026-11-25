#!/usr/bin/env bash
set -euo pipefail

state="$HOME/Library/Application Support/Remote Mac Host"
umask 077
mkdir -p "$state"
cert="$state/tls.crt"
key="$state/tls.key"
if [[ ! -e "$cert" && ! -e "$key" ]]; then
    stage="$(mktemp -d "$state/.tls.XXXXXX")"
    trap 'rm -rf "$stage"' EXIT
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -days 365 -subj /CN=remote-mac-lan -keyout "$stage/tls.key" -out "$stage/tls.crt"
    mv "$stage/tls.key" "$key"
    mv "$stage/tls.crt" "$cert"
elif [[ ! -f "$cert" || ! -f "$key" ]]; then
    echo "Incomplete TLS configuration in $state" >&2
    exit 1
fi
openssl x509 -in "$cert" -noout -fingerprint -sha256
