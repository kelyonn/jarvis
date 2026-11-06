#!/usr/bin/env bash
# Builds JarvisShell and wraps it into ~/Applications/Jarvis.app (or $OUT).
#
# macOS ties privacy grants (Mic, Accessibility, Screen Recording, ...) to the app's code signature.
# An ad-hoc signature changes on every build and silently revokes those grants, so this script uses a
# stable identity: $JARVIS_SIGN_IDENTITY, else the first "Apple Development"/"Developer ID" certificate,
# else it creates (once) a self-signed "Jarvis Local Signing" certificate in the login keychain.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/JarvisShell"
OUT="${OUT:-$HOME/Applications/Jarvis.app}"
CONFIG="${CONFIG:-release}"
ID="${JARVIS_SIGN_IDENTITY:-}"

find_identity() {
  security find-identity -v -p codesigning 2>/dev/null \
    | awk -F'"' '/Apple Development|Developer ID Application|Jarvis Local Signing/ {print $2; exit}'
}

create_self_signed() {
  local name="Jarvis Local Signing" tmp
  tmp="$(mktemp -d)"
  cat >"$tmp/openssl.cnf" <<EOF
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=$name
[ext]
basicConstraints=critical,CA:false
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
EOF
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -config "$tmp/openssl.cnf" \
    -keyout "$tmp/k.pem" -out "$tmp/c.pem" >/dev/null 2>&1
  openssl pkcs12 -export -inkey "$tmp/k.pem" -in "$tmp/c.pem" -out "$tmp/id.p12" -passout pass:jarvis >/dev/null 2>&1
  security import "$tmp/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" -P jarvis -T /usr/bin/codesign >/dev/null
  rm -rf "$tmp"
  echo "$name"
}

echo "==> swift build ($CONFIG)"
(cd "$PKG" && swift build -c "$CONFIG")
BIN="$(cd "$PKG" && swift build -c "$CONFIG" --show-bin-path)/JarvisShell"

echo "==> assembling $OUT"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$BIN" "$OUT/Contents/MacOS/JarvisShell"
cp "$PKG/Resources/Info.plist" "$OUT/Contents/Info.plist"

if [[ -z "$ID" ]]; then ID="$(find_identity || true)"; fi
if [[ -z "$ID" ]]; then
  echo "==> no signing identity found; creating a local self-signed one"
  ID="$(create_self_signed)"
fi
echo "==> signing with: $ID"
codesign --force --deep --options runtime --timestamp=none \
  --entitlements "$PKG/Resources/JarvisShell.entitlements" --sign "$ID" "$OUT"
codesign --verify --verbose=1 "$OUT" 2>&1 | tail -1
echo "Built $OUT"
