#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""images/ に置いた写真を index.html に流し込むスクリプト。

使い方（このファイルがある場所の1つ上、index.html と同じ場所で実行する）:

    python3 tools/apply_images.py --list    今どの枠に何が入っているかを見る
    python3 tools/apply_images.py           images/ の写真を index.html に反映する
    python3 tools/apply_images.py --revert  直前の状態（index.html.bak）に戻す

写真の置き方:
    images/ フォルダに「枠の名前.jpg」という名前で置くだけ。
    枠の名前は --list で確認できる（hero / about / ig1〜ig6）。
    拡張子は jpg / jpeg / png / webp のどれでもよい。

このスクリプトがやること:
    1. 大きすぎる写真を自動で縮める（横幅の上限は下の TARGET_WIDTH のとおり）
    2. images/optimized/ に軽くした写真を書き出す
    3. index.html の該当する枠に <img> を差し込む
    4. 書き換える前に index.html.bak を作る（--revert で1つ前に戻せる）

仮置きの図版は消さずに残してあり、写真が入っている間だけ CSS で隠れる。
そのため images/ から写真を消して実行し直せば、いつでも元の枠の見た目に戻る。
何度実行しても同じ結果になる。
"""

import os
import shutil
import sys

# ---- 設定（ここだけ直せば挙動が変わる）--------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # index.html がある場所
HTML = os.path.join(ROOT, "index.html")
SRC_DIR = os.path.join(ROOT, "images")           # 元の写真を置く場所
OUT_DIR = os.path.join(SRC_DIR, "optimized")     # 軽くした写真の書き出し先
BACKUP = HTML + ".bak"

# 枠ごとの横幅の上限（px）。これより大きい写真は自動で縮める。
# 画面での表示サイズの約2倍にしてある（スマホの高精細画面でぼやけないため）
TARGET_WIDTH = {"hero": 1800, "about": 1200, "ig": 700}   # ig は ig1〜ig6 に共通で効く
DEFAULT_WIDTH = 1200
JPEG_QUALITY = 82
WARN_KB = 250                                    # これを超えたら警告を出す

EXTS = (".jpg", ".jpeg", ".png", ".webp", ".JPG", ".JPEG", ".PNG", ".WEBP")
VENV_PY = os.path.expanduser("~/.venvs/gig-img/bin/python")


# ---- Pillow（画像を縮めるための部品）の準備 ---------------------------

def load_pillow():
    """Pillow を読み込む。この python に無ければ、入っている python で自分を実行し直す。"""
    try:
        from PIL import Image
        return Image
    except ImportError:
        pass
    # 専用の python（~/.venvs/gig-img）に Pillow が入っているので、そちらで実行し直す。
    # 環境変数で1回だけに制限する（無限に実行し直すのを防ぐため）
    if os.path.exists(VENV_PY) and os.environ.get("GIG_IMG_RETRY") != "1":
        env = dict(os.environ, GIG_IMG_RETRY="1")
        os.execve(VENV_PY, [VENV_PY, os.path.abspath(__file__)] + sys.argv[1:], env)
    return None


# ---- HTML を読み書きする部分 -----------------------------------------

def find_slots(html):
    """index.html から data-photo="..." が付いた枠を探して一覧で返す。

    戻り値は [{name, alt, decorative, open_start, open_end, close_start}, ...]
    （枠の中身を丸ごと入れ替えられるよう、開始タグと終了タグの位置を持つ）
    """
    slots = []
    pos = 0
    while True:
        i = html.find('data-photo="', pos)
        if i < 0:
            return slots
        # この属性が属する開始タグの「<」を左に探す
        lt = html.rfind("<", 0, i)
        gt = html.find(">", i)
        if lt < 0 or gt < 0:
            pos = i + 1
            continue
        open_tag = html[lt:gt + 1]
        tag = open_tag[1:].split()[0]            # div / li など
        name = attr_of(open_tag, "data-photo")
        alt = attr_of(open_tag, "data-photo-alt") or ""
        decorative = 'aria-hidden="true"' in open_tag  # 飾りの写真は alt を空にする
        close = matching_close(html, tag, gt + 1)
        if close is None:
            pos = gt + 1
            continue
        slots.append({
            "name": name, "alt": alt, "decorative": decorative,
            "open_start": lt, "open_end": gt + 1, "close_start": close,
        })
        pos = close


def attr_of(open_tag, key):
    k = key + '="'
    i = open_tag.find(k)
    if i < 0:
        return None
    i += len(k)
    return open_tag[i:open_tag.find('"', i)]


def matching_close(html, tag, start):
    """開始タグに対応する </tag> の位置を、入れ子を数えながら探す。"""
    depth = 1
    pos = start
    o, c = "<" + tag, "</" + tag + ">"
    while depth > 0:
        ni = html.find(o, pos)
        nc = html.find(c, pos)
        if nc < 0:
            return None
        # 同じタグが入れ子になっていたら深さを増やす
        if 0 <= ni < nc and html[ni + len(o)] in " >":
            depth += 1
            pos = ni + len(o)
        else:
            depth -= 1
            pos = nc + len(c)
            if depth == 0:
                return nc
    return None


def current_state(html, slot):
    inner = html[slot["open_end"]:slot["close_start"]]
    if "<img" in inner:
        src = inner.split('src="', 1)[1].split('"', 1)[0] if 'src="' in inner else "?"
        return "写真あり", src
    return "枠のまま", "-"


# ---- 写真を探す・縮める ----------------------------------------------

def find_source(name):
    for ext in EXTS:
        p = os.path.join(SRC_DIR, name + ext)
        if os.path.exists(p):
            return p
    return None


def optimize(src, name, Image):
    """写真を縮めて images/optimized/ に書き出し、そのパス（HTML から見た相対）を返す。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    # 枠の名前そのまま → 名前の頭（ig1 なら ig）→ 既定値、の順に探す
    width = TARGET_WIDTH.get(name) or TARGET_WIDTH.get(name.rstrip("0123456789")) or DEFAULT_WIDTH

    if Image is None:
        # Pillow が無い環境では、そのままコピーするだけ（縮められない）
        dst = os.path.join(OUT_DIR, os.path.basename(src))
        shutil.copyfile(src, dst)
        return dst, None

    im = Image.open(src)
    im = im.convert("RGB")
    if im.width > width:
        h = round(im.height * width / im.width)
        im = im.resize((width, h), Image.LANCZOS)
    dst = os.path.join(OUT_DIR, name + ".jpg")
    im.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return dst, im.size


# ---- 実行の入口 -------------------------------------------------------

def cmd_list(html):
    slots = find_slots(html)
    print("枠の一覧（images/ に「枠の名前.jpg」で置くと反映されます）\n")
    print("  %-8s %-10s %-28s %s" % ("枠の名前", "今の状態", "index.html での表示位置", "images/ の写真"))
    for s in slots:
        state, src = current_state(html, s)
        found = find_source(s["name"])
        print("  %-8s %-10s %-28s %s" % (
            s["name"], state, s["alt"] or "-",
            os.path.basename(found) if found else "（未配置）"))
    print("\n  images/ フォルダ: %s" % SRC_DIR)


# <img> を差し込むときに前に付ける改行。取り外すときも同じものを消すので、
# 「写真を入れる → 外す」を往復しても index.html は元の字下げのまま戻る
INDENT = "\n      "


def set_image(html, slot, rel, alt):
    """枠の中に <img> を差し込む（すでに入っていれば差し替える）。仮置きの図版は残す。"""
    inner = html[slot["open_end"]:slot["close_start"]]
    tag = '<img src="%s" alt="%s">' % (rel, alt)
    i = inner.find("<img")
    if i >= 0:
        j = inner.find(">", i) + 1
        inner = inner[:i] + tag + inner[j:]
    else:
        inner = INDENT + tag + inner
    return html[:slot["open_end"]] + inner + html[slot["close_start"]:]


def clear_image(html, slot):
    """枠から <img> を取り外す（仮置きの図版が再び表示される）。"""
    inner = html[slot["open_end"]:slot["close_start"]]
    i = inner.find("<img")
    if i < 0:
        return html, False
    j = inner.find(">", i) + 1
    inner = inner[:i] + inner[j:]
    if inner.startswith(INDENT):
        inner = inner[len(INDENT):]
    return html[:slot["open_end"]] + inner + html[slot["close_start"]:], True


def cmd_apply(html, Image):
    slots = find_slots(html)
    todo = [s for s in slots if find_source(s["name"])]
    stale = [s for s in slots if not find_source(s["name"])
             and current_state(html, s)[0] == "写真あり"]

    if not todo and not stale:
        print("images/ に写真が見つかりませんでした。")
        print("枠の名前は次のコマンドで確認できます: python3 tools/apply_images.py --list")
        print("置き場所: %s" % SRC_DIR)
        return 0

    if todo and Image is None:
        print("※ 画像を縮める部品（Pillow）が見つからないため、そのままの大きさで組み込みます。")
        print("  重い写真は https://squoosh.app/ で横幅1800px以下・250KB以下にしてから置き直してください。\n")

    shutil.copyfile(HTML, BACKUP)

    # 後ろの枠から書き換える（前から書き換えると位置がずれるため）
    for s in sorted(slots, key=lambda x: x["open_start"], reverse=True):
        src = find_source(s["name"])
        if src:
            dst, size = optimize(src, s["name"], Image)
            rel = os.path.relpath(dst, ROOT).replace(os.sep, "/")
            kb = os.path.getsize(dst) // 1024
            alt = "" if s["decorative"] else s["alt"]
            html = set_image(html, s, rel, alt)
            note = "%dx%d" % size if size else "サイズそのまま"
            mark = "  ⚠ 重いので圧縮を検討" if kb > WARN_KB else ""
            print("  %-8s ← %-24s %s / %dKB%s" % (s["name"], os.path.basename(src), note, kb, mark))
        elif s in stale:
            html, removed = clear_image(html, s)
            if removed:
                print("  %-8s   images/ に写真が無くなったので、元の枠に戻しました" % s["name"])

    # ファーストビューに写真があるときだけ、装飾用の薄さを解除する印を付ける
    hero_has_photo = any(s["name"] == "hero" for s in todo)
    if hero_has_photo and "hero--has-photo" not in html:
        html = html.replace('<section class="hero">', '<section class="hero hero--has-photo">', 1)
    elif not hero_has_photo:
        html = html.replace('<section class="hero hero--has-photo">', '<section class="hero">', 1)

    with open(HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("\n反映しました（写真 %d 枚）。" % len(todo))
    print("1つ前に戻したいときは: python3 tools/apply_images.py --revert")
    print("ブラウザで index.html を開いて、390px / 820px / 1440px の3つの幅で確認してください。")
    return 0


def cmd_revert():
    if not os.path.exists(BACKUP):
        print("戻せる控え（index.html.bak）がありません。")
        print("git を使っている場合は次のコマンドでも戻せます: git checkout index.html")
        return 1
    shutil.copyfile(BACKUP, HTML)
    print("index.html を直前の状態に戻しました。")
    return 0


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if not os.path.exists(HTML):
        print("index.html が見つかりません: %s" % HTML)
        print("index.html と同じ場所で実行してください。")
        return 1
    if "--revert" in args:
        return cmd_revert()

    with open(HTML, encoding="utf-8") as f:
        html = f.read()
    if "--list" in args:
        cmd_list(html)
        return 0
    return cmd_apply(html, load_pillow())


if __name__ == "__main__":
    sys.exit(main())
