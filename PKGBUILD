# Maintainer: envy <envy@localhost>
#
# This PKGBUILD sources the script from the directory next to it. If you
# publish the project to a git host, switch to the hosted form:
#   source=("git+https://github.com/you/tuiplayer.git#tag=v$pkgver")
#   sha256sums=('SKIP')
# and add `git` to makedepends.

pkgname=tuiplayer
pkgver=0.3.0
pkgrel=1
pkgdesc="Barebones keyboard-driven TUI music player that races free sources for fast playback"
arch=('any')
url="https://example.com/tuiplayer"
license=('MIT')
depends=('python' 'mpv' 'yt-dlp')
source=("tuiplayer.py"
        "tuiplayer.1"
        "README.md"
        "LICENSE")
sha256sums=('SKIP'
            'SKIP'
            'SKIP'
            'SKIP')

build() {
  # byte-compile as a sanity check; the installed artifact stays the script
  python -m py_compile "$srcdir/tuiplayer.py"
}

check() {
  python "$srcdir/tuiplayer.py" --version | grep -q "tuiplayer $pkgver"
}

package() {
  install -Dm755 "$srcdir/tuiplayer.py" "$pkgdir/usr/bin/tuiplayer"
  install -Dm644 "$srcdir/tuiplayer.1" "$pkgdir/usr/share/man/man1/tuiplayer.1"
  install -Dm644 "$srcdir/README.md"   "$pkgdir/usr/share/doc/tuiplayer/README.md"
  install -Dm644 "$srcdir/LICENSE"     "$pkgdir/usr/share/licenses/tuiplayer/LICENSE"
}
