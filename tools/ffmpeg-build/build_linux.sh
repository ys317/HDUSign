#!/usr/bin/env bash
# Run on Linux x86_64 after fetch_sources.py. This never publishes anything.
set -euo pipefail

test "$(uname -s)" = "Linux" || { echo 'Use Linux to run build_linux.sh.' >&2; exit 1; }
test "$(uname -m)" = "x86_64" || { echo 'Linux x86_64 is required.' >&2; exit 1; }
recipe_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
version=8.1.2
signer=FCF986EA15E6E293A5644F10B4322F04D67658D8
mkdir -p "$recipe_dir/work"
build_root=$(mktemp -d "$recipe_dir/work/build.XXXXXX")
record="$build_root/record"
mkdir -p "$record" "$build_root/gnupg" "$build_root/out"
chmod 700 "$build_root/gnupg"

cd "$recipe_dir/downloads"
sha256sum --check SHA256SUMS
gpg --homedir "$build_root/gnupg" --batch --no-autostart --no-auto-key-retrieve \
    --import ffmpeg-release-key.asc
gpg --homedir "$build_root/gnupg" --batch --no-autostart --no-auto-key-retrieve \
    --status-fd 1 --verify "ffmpeg-$version.tar.xz.asc" "ffmpeg-$version.tar.xz" \
    > "$record/source-signature.txt"
grep -F "[GNUPG:] VALIDSIG $signer " "$record/source-signature.txt" >/dev/null
tar --extract --file "ffmpeg-$version.tar.xz" --directory "$build_root" --no-same-owner

# No external codec/compression libraries are enabled or auto-detected.
# GnuTLS provides TLS verification via certifi's CA bundle on Linux.
# AAC encoding is included for HDUSign's existing offline runtime check.
configure=(
    --target-os=linux --arch=x86_64 --cc=gcc --cxx=g++
    --disable-autodetect --disable-everything --disable-programs --enable-ffmpeg
    --disable-gpl --disable-nonfree --disable-version3
    --disable-shared --enable-static --disable-debug --disable-doc
    --disable-avdevice --disable-swscale --enable-pthreads
    --enable-avcodec --enable-avformat --enable-avfilter --enable-swresample
    --enable-network --enable-gnutls
    --disable-openssl --disable-schannel --disable-mbedtls --disable-libtls
    --disable-bzlib --disable-zlib --disable-lzma --disable-iconv
    --enable-protocol=file,pipe,http,https,tcp,tls,httpproxy,udp
    --enable-demuxer=flv,wav,aac,matroska,mov,mp3,flac,ogg
    --enable-muxer=flv,adts,pcm_f32le,wav
    --enable-decoder=aac,aac_latm,ac3,eac3,alac,flac,mp3,mp3float,opus,vorbis
    --enable-decoder=pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_u8,pcm_f32le,pcm_f64le,adpcm_swf,nellymoser,speex
    --enable-encoder=aac,pcm_f32le
    --enable-parser=aac,aac_latm,ac3,flac,mpegaudio,opus,vorbis
    --enable-filter=aformat,aresample,anull
    --extra-version=hdusign-audio
)
printf '%s\n' "${configure[@]}" > "$record/configure-args.txt"
if command -v dpkg-query >/dev/null 2>&1; then
    dpkg-query -W -f='${Package}=${Version}\n' > "$record/dpkg-packages.txt" || true
fi
gcc --version > "$record/gcc-version.txt"
ld --version > "$record/ld-version.txt"
nasm --version > "$record/nasm-version.txt"
export SOURCE_DATE_EPOCH=1781664539
cd "$build_root/out"
../"ffmpeg-$version"/configure "${configure[@]}" 2>&1 | tee "$record/configure-output.txt"
make -j"$(nproc)" ffmpeg 2>&1 | tee "$record/build-output.txt"
cp config.h config_components.h "$record/"
cp ffbuild/config.mak ffbuild/config.log "$record/"
readelf -d ffmpeg > "$record/elf-imports.txt"

# Preserve copyright/license notices for linked runtime libraries.
mkdir -p "$record/toolchain-licenses"
for pkg in libgcc-s1 libc6 libgnutls30; do
    for candidate in "/usr/share/doc/$pkg/copyright" "/usr/share/doc/$pkg:amd64/copyright"; do
        if [[ -f "$candidate" ]]; then
            mkdir -p "$record/toolchain-licenses/$pkg"
            cp "$candidate" "$record/toolchain-licenses/$pkg/copyright"
            break
        fi
    done
done
test -n "$(find "$record/toolchain-licenses" -type f -print -quit)"
echo "$build_root" > "$recipe_dir/work/build-path.txt"
echo 'FFmpeg built. Run verify.py before package.py.'
