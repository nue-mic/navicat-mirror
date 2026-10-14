"""Navicat 安装包镜像：探测官方下载地址 → 按版本建 Release → 逐个下载上传为 assets。

用法:
    python mirror.py sync [--dry-run]       探测并同步缺失的安装包
    python mirror.py backfill [--dry-run]   从 Wayback Machine 快照回填已被官方覆盖的旧版本
    python mirror.py readme                 根据 Releases 重新生成 README.md
依赖: Python 3 标准库 + gh CLI（需 GH_TOKEN、GITHUB_REPOSITORY 环境变量）
"""
import concurrent.futures as cf
import datetime as dt
import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

CDN = "https://dn.navicat.com/download/"
WAYBACK = "https://web.archive.org/"
MIN_SIZE = 20 << 20  # Wayback 里有官方早年的约 1 MiB 在线安装器，不是完整安装包
BACKFILL = {("premium", "windows"), ("premium", "macos")}  # 回填只取最常用的组合，控制体积
CDN_HOSTS = ["dn.navicat.com", "download.navicat.com", "download3.navicat.com"]
NOTES_URL = "https://www.navicat.com/en/products/navicat-premium-release-note"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"}
REPO = os.environ.get("GITHUB_REPOSITORY", "nue-mic/navicat-mirror")

# 发布 slug: (文件名中的 slug, AppImage 中的 slug, 显示名)
PRODUCTS = {
    "premium": ("premium", "premium", "Navicat Premium"),
    "premium-lite": ("premium_lite", "premium-lite", "Navicat Premium Lite"),
    "mysql": ("mysql", "mysql", "Navicat for MySQL"),
    "postgresql": ("pgsql", "pgsql", "Navicat for PostgreSQL"),
    "oracle": ("ora", "ora", "Navicat for Oracle"),
    "sqlite": ("sqlite", "sqlite", "Navicat for SQLite"),
    "sqlserver": ("sqlserver", "sqlserver", "Navicat for SQL Server"),
    "mariadb": ("mariadb", "mariadb", "Navicat for MariaDB"),
    "mongodb": ("mongodb", "mongodb", "Navicat for MongoDB"),
    "redis": ("redis", "redis", "Navicat for Redis"),
    "snowflake": ("snowflake", "snowflake", "Navicat for Snowflake"),
}
LANGS = ["en", "cs"]
MAJORS = range(15, 20)  # 含下一个大版本，官方一发布即可探测到
PATTERNS = [
    "navicat{c}_{p}_{l}_x64.exe", "navicat{c}_{p}_{l}_x86.exe", "navicat{c}_{p}_{l}_arm64.exe",
    "navicat{c}_{p}_{l}.dmg", "navicat{c}_{p}_{l}_arm64.dmg", "navicat{c}_{p}_{l}_x86_64.dmg",
    "navicat{c}-{a}-{l}.AppImage", "navicat{c}-{a}-{l}-x86_64.AppImage", "navicat{c}-{a}-{l}-aarch64.AppImage",
]
PLATFORMS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
DEFER_GAP = dt.timedelta(days=3)  # 文件比最新更新日志晚这么多天，视为日志未跟上
DEFER_MAX = dt.timedelta(days=7)  # 最多等日志这么久，之后按日期命名


def http(url, method="GET"):
    return urllib.request.urlopen(urllib.request.Request(url, method=method, headers=UA), timeout=60)


def head(name):
    for _ in range(3):
        try:
            with http(CDN + name, "HEAD") as r:
                lm = email.utils.parsedate_to_datetime(r.headers["Last-Modified"])
                return {"file": name, "size": int(r.headers["Content-Length"]), "lm": lm}
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
        except Exception:
            pass
    return None


def parallel_head(names):
    with cf.ThreadPoolExecutor(16) as ex:
        return [r for r in ex.map(head, names) if r]


def all_combos():
    return [(c, pat) for m in MAJORS for c in [str(m)] + [f"{m}{i}" for i in range(10)] for pat in PATTERNS]


def candidates(product, lang, combos):
    p, a, _ = PRODUCTS[product]
    return [pat.format(c=c, p=p, a=a, l=lang) for c, pat in combos]


def discover():
    """以 Premium/en 全量探测出存在的 (版本线代号, 文件名模式)，其他产品/语言只探测这些组合。"""
    combos = all_combos()
    found = {r["file"] for r in parallel_head(candidates("premium", "en", combos))}
    combos = [cp for cp in combos if candidates("premium", "en", [cp])[0] in found]
    jobs = []
    for product in PRODUCTS:
        for lang in LANGS:
            for (code, pat), name in zip(combos, candidates(product, lang, combos)):
                jobs.append((product, lang, code, name))
    heads = {r["file"]: r for r in parallel_head([j[3] for j in jobs])}
    items = []
    for product, lang, code, name in jobs:
        if name in heads:
            items.append({**heads[name], "product": product, "lang": lang, "code": code,
                          "platform": platform_of(name)})
    return items


def wayback_head(ts, url):
    """快照完整 → 返回原始 Last-Modified/大小；截断的快照 Wayback 会跳到其他时间点，这里一并校验。"""
    for _ in range(3):
        try:
            with http(f"{WAYBACK}web/{ts}id_/{url}", "HEAD") as r:
                size, orig = r.headers.get("Content-Length"), r.headers.get("x-archive-orig-content-length")
                lm = r.headers.get("x-archive-orig-last-modified")
                if not (size and lm and size == orig):
                    return None
                return {"size": int(size), "lm": email.utils.parsedate_to_datetime(lm), "url": r.url}
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
        except Exception:
            pass
    return None


def discover_wayback():
    """列出官方下载地址在 Wayback 的全部快照（按内容去重），只保留已知产品/语言/版本线的文件名。"""
    known = {}
    combos = all_combos()
    for product in PRODUCTS:
        for lang in LANGS:
            for (code, _), name in zip(combos, candidates(product, lang, combos)):
                if (product, platform_of(name)) in BACKFILL:
                    known[name] = (product, lang, code)
    snaps = []
    for host in CDN_HOSTS:
        q = urllib.parse.urlencode({"url": f"{host}/download/navicat1", "matchType": "prefix",
                                    "fl": "timestamp,original", "filter": "statuscode:200", "collapse": "digest"})
        with http(f"{WAYBACK}cdx/search/cdx?{q}") as r:
            for line in r.read().decode().splitlines():
                ts, url = line.split(" ", 1)
                if url.rsplit("/", 1)[1] in known:
                    snaps.append((ts, url))
    print(f"Wayback 共 {len(snaps)} 个候选快照，逐个校验完整性")
    with cf.ThreadPoolExecutor(4) as ex:  # Wayback 限流较严，并发不宜高
        heads = list(ex.map(lambda s: wayback_head(*s), snaps))
    items, seen = [], set()
    for (_, url), h in zip(snaps, heads):
        name = url.rsplit("/", 1)[1]
        if not h or h["size"] < MIN_SIZE or (name, h["size"]) in seen:
            continue
        seen.add((name, h["size"]))
        product, lang, code = known[name]
        items.append({**h, "file": name, "product": product, "lang": lang, "code": code,
                      "platform": platform_of(name)})
    return items


def platform_of(name):
    return {"exe": "windows", "dmg": "macos", "AppImage": "linux"}[name.rsplit(".", 1)[1]]


def load_notes():
    """解析 Premium 更新日志 → [(平台, (主,次,修订), 日期)]，各产品同平台版本号一致。"""
    with http(NOTES_URL) as r:
        html = r.read().decode("utf-8", "replace")
    notes = []
    for date_s, title in re.findall(r"note-date'><b>([^<]+)</b>.*?note-title'><b>([^<]+)</b>", html, re.S):
        m = re.search(r"version (\d+)\.(\d+)\.(\d+)", title)
        t = title.lower()
        plat = "windows" if "win" in t else "macos" if "mac" in t else "linux" if "linux" in t else None
        try:
            date = dt.datetime.strptime(date_s.strip(), "%b %d %Y").date()
        except ValueError:
            continue
        if m and plat:
            notes.append((plat, tuple(map(int, m.groups())), date))
    return notes


def pick_version(item, notes, now):
    """取更新日志中发布日期离文件 Last-Modified 最近的版本（官方常先传文件、几天后才发日志）。
    文件明显新于最新日志且刚上传 → 返回 None 暂缓，等日志跟上。"""
    code = item["code"]
    major, minor = int(code[:2]), (int(code[2]) if len(code) > 2 else None)
    lm = item["lm"].date()
    line = [(v, d) for p, v, d in notes
            if p == item["platform"] and v[0] == major and (minor is None or v[1] == minor)]
    if not line:
        return f"{major}.{'x' if minor is None else minor}-{lm:%Y%m%d}"
    if lm - max(d for _, d in line) > DEFER_GAP and now - item["lm"] < DEFER_MAX:
        return None
    v, _ = min(line, key=lambda vd: (abs((vd[1] - lm).days), [-x for x in vd[0]]))
    return ".".join(map(str, v))


def gh(*args, capture=True):
    return subprocess.run(["gh", *args], check=True, text=True, encoding="utf-8",
                          capture_output=capture).stdout


def list_releases():
    """返回 {tag: {asset名: size}}；仓库不存在或无 Release 时返回空。"""
    try:
        out = gh("api", f"repos/{REPO}/releases", "--paginate",
                 "--jq", ".[] | {tag: .tag_name, assets: [.assets[] | {name, size, url: .browser_download_url}]}")
    except subprocess.CalledProcessError:
        return {}
    rel = {}
    for line in out.splitlines():
        r = json.loads(line)
        rel[r["tag"]] = {a["name"]: a for a in r["assets"]}
    return rel


def asset_name(item, existing):
    """同名且同大小 → 已镜像返回 None；同名不同大小（官方重打包）→ 加日期后缀另存。"""
    name = item["file"]
    if name not in existing:
        return name
    if existing[name]["size"] == item["size"]:
        return None
    stem, ext = name.rsplit(".", 1)
    dated = f"{stem}-{item['lm']:%Y%m%d}.{ext}"
    return None if dated in existing else dated


def download(url, path, size):
    with http(url) as r, open(path, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    if os.path.getsize(path) != size:
        raise IOError(f"大小不符: {os.path.getsize(path)} != {size}")


def sync(dry_run, backfill=False):
    now = dt.datetime.now(dt.timezone.utc)
    notes = load_notes()
    items = discover_wayback() if backfill else discover()
    releases = list_releases()
    print(f"探测到 {len(items)} 个{'Wayback 快照' if backfill else '官方文件'}，已有 {len(releases)} 个 Release")
    failed = 0
    for it in sorted(items, key=lambda i: (i["product"], i["platform"], i["code"], i["file"], i["lm"])):
        version = pick_version(it, notes, now)
        if version is None:
            print(f"[暂缓] {it['file']} 更新于 {it['lm']:%F}，更新日志尚未收录新版本")
            continue
        tag = f"{it['product']}-{it['platform']}-{version}"
        name = asset_name(it, releases.get(tag, {}))
        if name is None:
            continue
        print(f"[同步] {tag} <- {it['file']} ({it['size'] / 2**20:.0f} MiB, {it['lm']:%F}) 作为 {name}")
        if dry_run:
            releases.setdefault(tag, {})[name] = {"name": name, "size": it["size"]}
            continue
        try:
            if tag not in releases:
                title = f"{PRODUCTS[it['product']][2]} {version} ({PLATFORMS[it['platform']]})"
                body = (f"{title} 官方安装包镜像。\n\n"
                        f"- 文件名与官方一致，官方地址 `{CDN}<文件名>`（官方会覆盖为新版本，旧版仅此处留存）\n"
                        f"- 版本号依据官方更新日志与文件发布时间匹配得出\n"
                        f"- 官网下载页: https://www.navicat.com/en/download/navicat-premium")
                if "url" in it:
                    body += "\n- 旧版本已被官方覆盖，安装包取自 Wayback Machine 对官方下载地址的存档快照（已校验大小完整）"
                gh("release", "create", tag, "--repo", REPO, "--title", title, "--notes", body, "--latest=false")
                releases[tag] = {}
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, name)
                download(it.get("url", CDN + it["file"]), path, it["size"])
                gh("release", "upload", tag, path, "--repo", REPO, "--clobber")
            releases[tag][name] = {"name": name, "size": it["size"]}
        except Exception as e:
            failed += 1
            print(f"[失败] {it['file']}: {e}", file=sys.stderr)
    print(f"完成，失败 {failed} 个")
    return 1 if failed else 0


def vkey(version):
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.-]", version))


def readme():
    releases = list_releases()
    total = sum(a["size"] for assets in releases.values() for a in assets.values())
    lines = [
        "# Navicat 安装包镜像",
        "",
        "> [!TIP]",
        "> 由 GitHub Actions 每 6 小时自动探测官方下载地址，发现新安装包即下载并上传到 Releases 永久留存。",
        "> 官方下载地址只按版本线命名、会被新版本覆盖，旧版本只能在这里找到。",
        "",
        f"- 覆盖产品: {len(PRODUCTS)} 款桌面产品；语言: {' / '.join(LANGS)}；版本线: 15 / 16.0~16.3 / 17 / 18",
        f"- 平台: Windows (x64/x86) · macOS (Universal/Apple Silicon/Intel) · Linux AppImage (x86_64/aarch64)",
        f"- 当前收录 {len(releases)} 个 Release，共 {total / 2**30:.1f} GiB",
        f"- 每个 Release 对应「产品 × 平台 × 版本」，tag 形如 `premium-windows-18.0.3`",
        "",
    ]
    for slug, (_, _, display) in PRODUCTS.items():
        rows = []
        for tag, assets in releases.items():
            m = re.fullmatch(rf"{re.escape(slug)}-(windows|macos|linux)-(.+)", tag)
            if m and assets:
                rows.append((vkey(m.group(2)), m.group(2), m.group(1), tag, assets))
        if not rows:
            continue
        lines += [f"## {display}", "", "| 版本 | 平台 | 安装包 |", "| :--- | :--- | :--- |"]
        for _, version, plat, tag, assets in sorted(rows, key=lambda r: (r[0], r[2]), reverse=True):
            links = " · ".join(f"[{n}]({a['url']}) ({a['size'] / 2**20:.0f}M)" for n, a in sorted(assets.items()))
            lines.append(f"| [`{version}`](https://github.com/{REPO}/releases/tag/{tag}) | {PLATFORMS[plat]} | {links} |")
        lines.append("")
    lines += ["---", "本仓库仅镜像官方公开发布的原版安装包，版权归 PremiumSoft CyberTech Ltd. 所有。", ""]
    with open("README.md", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("sync", "backfill"):
        sys.exit(sync("--dry-run" in sys.argv, cmd == "backfill"))
    elif cmd == "readme":
        readme()
    else:
        print(__doc__)
        sys.exit(2)
