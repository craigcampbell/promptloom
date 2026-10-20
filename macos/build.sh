#!/usr/bin/env bash
# Builds Promptloom.app (the menu bar indicator) with plain swiftc. Needs the
# Xcode Command Line Tools (`xcode-select --install`), not Xcode itself.
# Usage: macos/build.sh [output dir]   (default: macos/build)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/build}"
app="$out/Promptloom.app"
version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$here/../pyproject.toml" | head -1)"
version="${version:-0.0.0}"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

# One binary for both Apple silicon and Intel.
for arch in arm64 x86_64; do
    swiftc -O -parse-as-library -swift-version 5 \
        -target "$arch-apple-macos13.0" \
        "$here"/PromptloomBar/Sources/*.swift \
        -o "$out/Promptloom-$arch"
done
lipo -create "$out/Promptloom-arm64" "$out/Promptloom-x86_64" -output "$app/Contents/MacOS/Promptloom"
rm -f "$out/Promptloom-arm64" "$out/Promptloom-x86_64"

cat > "$app/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>io.github.craigcampbell.promptloom</string>
    <key>CFBundleName</key>
    <string>Promptloom</string>
    <key>CFBundleDisplayName</key>
    <string>Promptloom</string>
    <key>CFBundleExecutable</key>
    <string>Promptloom</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$version</string>
    <key>CFBundleVersion</key>
    <string>$version</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

# Ad-hoc signature: enough for Apple silicon to run a locally built app.
codesign --force --sign - "$app" >/dev/null 2>&1 || true
echo "$app"
