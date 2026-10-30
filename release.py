#!/usr/bin/env python3
"""release.py — сборка и публикация релиза KaraokePlayer."""
import os, re, sys, subprocess, shutil
from pathlib import Path

PROJECT_DIR = Path.home() / "Desktop" / "Karaoke"
KARAOKE_PY = PROJECT_DIR / "karaoke.py"
PYTHON = "/opt/local/bin/python3.12"
VLC_LIB = "/Applications/VLC.app/Contents/MacOS/lib"
GITHUB_REPO = "SteepCanyon/karaoke.py"


def run(cmd, env=None, check=True):
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=PROJECT_DIR, env=env)
    if check and r.returncode != 0:
        sys.exit(f"❌ Команда завершилась с кодом {r.returncode}")
    return r


def read_version():
    src = KARAOKE_PY.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
    if not m:
        sys.exit("❌ APP_VERSION не найден в karaoke.py")
    return m.group(1)


def bump_version(current, arg):
    parts = [int(x) for x in re.findall(r"\d+", current)]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if arg == "major":
        major, minor, patch = major + 1, 0, 0
    elif arg == "minor":
        minor, patch = minor + 1, 0
    elif arg == "patch":
        patch += 1
    elif re.match(r"^\d+\.\d+\.\d+$", arg):
        return arg
    else:
        sys.exit(f"❌ Неверный аргумент: {arg}. Ожидается X.Y.Z, major, minor или patch")
    return f"{major}.{minor}.{patch}"


def update_version_in_file(new_version):
    src = KARAOKE_PY.read_text(encoding="utf-8")
    src = re.sub(r'APP_VERSION\s*=\s*"[^"]+"',
                 f'APP_VERSION = "{new_version}"', src)
    KARAOKE_PY.write_text(src, encoding="utf-8")
    print(f"✅ APP_VERSION = {new_version}")


def check_prerequisites():
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("❌ gh не авторизован. Выполните: gh auth login")
    if not (PROJECT_DIR / "zapret" / "tpws" / "tpws").exists():
        sys.exit("❌ zapret/tpws/tpws не найден")
    if not Path(VLC_LIB).exists():
        sys.exit(f"❌ VLC не найден: {VLC_LIB}")


def build():
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = VLC_LIB
    for p in ("build", "dist", "KaraokePlayer.spec"):
        fp = PROJECT_DIR / p
        if fp.is_dir():
            shutil.rmtree(fp)
        elif fp.is_file():
            fp.unlink()
    cmd = [
        PYTHON, "-m", "PyInstaller",
        "--windowed", "--name", "KaraokePlayer",
        "--osx-bundle-identifier", "com.suncho.karaokeplayer",
        "--add-data", "zapret/tpws/tpws:zapret/tpws",
        "--hidden-import", "vlc",
        "--hidden-import", "yt_dlp",
    ]
    if (PROJECT_DIR / "AppIcon.icns").exists():
        cmd += ["--icon", "AppIcon.icns"]
    cmd.append("karaoke.py")
    run(cmd, env=env)


def build_dmg(version):
    settings = PROJECT_DIR / "dmg_settings.py"
    settings.write_text('''import os.path
application = defines.get("app", "dist/KaraokePlayer.app")
appname = os.path.basename(application)
files = [application]
symlinks = {"Applications": "/Applications"}
icon = "AppIcon.icns"
icon_locations = {appname: (140, 120), "Applications": (500, 120)}
background = "builtin-arrow"
window_rect = ((100, 100), (640, 280))
''', encoding="utf-8")
    dmg_name = f"KaraokePlayer-{version}.dmg"
    dmg_path = PROJECT_DIR / dmg_name
    if dmg_path.exists():
        dmg_path.unlink()
    run([PYTHON, "-m", "dmgbuild",
         "-s", "dmg_settings.py",
         f"KaraokePlayer {version}", dmg_name])
    if not dmg_path.exists():
        sys.exit(f"❌ .dmg не создан: {dmg_path}")
    return dmg_path


def git_commit_and_tag(version):
    tag = f"v{version}"

    # Удаляем локальный тег, если есть
    r = subprocess.run(["git", "tag", "-l", tag],
                      capture_output=True, text=True, cwd=PROJECT_DIR)
    if r.stdout.strip() == tag:
        print(f"⚠️  Тег {tag} уже существует локально, удаляю")
        subprocess.run(["git", "tag", "-d", tag], cwd=PROJECT_DIR)

    # Stage
    run(["git", "add", "-A"])  # добавляем всё, что не в .gitignore

    # Проверяем, есть ли изменения для коммита
    r = subprocess.run(["git", "diff", "--cached", "--quiet"],
                      cwd=PROJECT_DIR)
    if r.returncode == 0:
        print("ℹ️  Нет изменений для коммита — создаём тег на текущем HEAD")
    else:
        r = subprocess.run(["git", "commit", "-m", f"Release {tag}"],
                          capture_output=True, text=True, cwd=PROJECT_DIR)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr)
            sys.exit("❌ git commit failed")

    # Push коммит (если был)
    r = subprocess.run(["git", "push"],
                      capture_output=True, text=True, cwd=PROJECT_DIR)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit("❌ git push failed")

    # Push тег
    run(["git", "tag", tag])
    r = subprocess.run(["git", "push", "origin", tag],
                      capture_output=True, text=True, cwd=PROJECT_DIR)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        print(f"⚠️  Не удалось запушить тег {tag} (возможно, уже есть на remote).")
        print(f"   Удалите его: git push origin :refs/tags/{tag}")
        sys.exit(1)
    return tag


def gh_release(version, tag, dmg_path):
    r = subprocess.run(["gh", "release", "view", tag],
                      capture_output=True, text=True, cwd=PROJECT_DIR)
    if r.returncode == 0:
        print(f"⚠️  Релиз {tag} уже существует — обновляю .dmg")
        run(["gh", "release", "upload", tag, str(dmg_path), "--clobber"])
    else:
        run(["gh", "release", "create", tag, str(dmg_path),
             "--title", f"KaraokePlayer {version}",
             "--notes", f"KaraokePlayer {version}"])


def main():
    if len(sys.argv) < 2:
        print("Использование:")
        print("  release.py 1.0.1     — конкретная версия")
        print("  release.py patch     — X.Y.Z → X.Y.Z+1")
        print("  release.py minor     — X.Y.Z → X.Y+1.0")
        print("  release.py major     — X.Y.Z → X+1.0.0")
        sys.exit(1)
    check_prerequisites()
    current = read_version()
    new = bump_version(current, sys.argv[1])
    if current == new:
        print(f"⚠️  APP_VERSION уже {current}. Пропускаю пересборку.")
        print(f"   Если хотите пересобрать тот же .dmg — удалите текущий релиз и тег на GitHub,")
        print(f"   затем выполните: git tag -d v{current} && git push origin :refs/tags/v{current}")
        print(f"   Затем: release.py {current}")
        input("Enter — всё равно продолжить, Ctrl+C — отмена ")
    else:
        print(f"\n📦 Релиз: {current} → {new}")
        input("Enter — продолжить, Ctrl+C — отмена ")
    update_version_in_file(new)
    print("\n🔨 Сборка .app...")
    build()
    print("\n💿 Сборка .dmg...")
    dmg_path = build_dmg(new)
    print(f"\n✅ Собрано: {dmg_path.name} "
          f"({dmg_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print("\n📤 Git push + tag...")
    tag = git_commit_and_tag(new)
    print("\n🚀 GitHub Release...")
    gh_release(new, tag, dmg_path)
    print(f"\n✅ Готово: https://github.com/{GITHUB_REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
