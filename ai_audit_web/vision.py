import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .paths import DATA_DIR as ROOT
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

try:
    from PIL import Image
    import numpy as np
    PIL_OK = True
except Exception:
    PIL_OK = False
    np = None

try:
    import cv2
    CV2_OK = True
    FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
except Exception:
    CV2_OK = False
    FACE_CASCADE = None

IMAGE_MAGICS = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM", b"RIFF")

FIO_RE = re.compile(
    r"[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}")

# ===== ЦЕЛЕВОЙ РАЗМЕР ПОРТРЕТА (меняй здесь) =====
TARGET_W = 714
TARGET_H = 952
TARGET_TOL = 0.12  # допуск 12% к целевым размерам

DEFAULT_STANDARD = {
    "min_width": 400, "min_height": 400,
    "face_cx": [0.30, 0.70], "face_cy": [0.15, 0.65],
}


# ============================================================
# Пути
# ============================================================

def _img_dir(site_id):
    p = ROOT / "storage" / str(site_id) / "images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _metrics_path(site_id):
    return _img_dir(site_id) / "photo_metrics.json"


def _manifest_path(site_id):
    return ROOT / "storage" / str(site_id) / "images_manifest.json"


def _report_path(site_id):
    return ROOT / "storage" / str(site_id) / "photo_report.json"


def _ref_dir():
    p = ROOT / "storage" / "reference"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_photo_report(site_id):
    p = _report_path(site_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def load_metrics(site_id):
    p = _metrics_path(site_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_manifest(site_id):
    p = _manifest_path(site_id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            return []
    return []


# ============================================================
# Эталон
# ============================================================

def save_reference_bytes(data, name):
    ext = Path(name).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    for old in _ref_dir().glob("photo.*"):
        try:
            old.unlink()
        except Exception:
            pass
    path = _ref_dir() / ("photo" + ext)
    path.write_bytes(data)
    m = deterministic_metrics(path)
    (_ref_dir() / "metrics.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return m


def get_reference_info():
    p = _ref_dir() / "metrics.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


# ============================================================
# Метрики
# ============================================================

def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = []
    for row in range(size):
        for col in range(size):
            a = px[row * (size + 1) + col]
            b = px[row * (size + 1) + col + 1]
            bits.append(1 if a > b else 0)
    return int("".join(map(str, bits)), 2)


def hamming(a, b):
    return bin(a ^ b).count("1")


def deterministic_metrics(path):
    if not PIL_OK:
        raise RuntimeError("Pillow не установлен")
    img = Image.open(path)
    img.load()
    w, h = img.size
    gray = np.array(img.convert("L"), dtype=np.float32)
    std = float(gray.std())
    uniq = len(np.unique((gray.astype(np.uint8) // 8)))

    face = None
    if CV2_OK and FACE_CASCADE is not None and not FACE_CASCADE.empty():
        g8 = np.array(img.convert("L"))
        found = FACE_CASCADE.detectMultiScale(g8, 1.15, 6, minSize=(30, 30))
        if len(found):
            x, y, fw, fh = max(found, key=lambda f: f[2] * f[3])
            face = {"cx": round(float(x + fw / 2) / w, 2),
                    "cy": round(float(y + fh / 2) / h, 2),
                    "fh": round(float(fh) / h, 2)}

    return {
        "width": w, "height": h, "ratio": round(w / h, 2) if h else 0,
        "orientation": "portrait" if h > w else ("landscape" if w > h else "square"),
        "std": round(std, 1), "solid": bool(std < 12 or uniq < 10),
        "dhash": dhash(img), "face": face,
        "size_kb": round(Path(path).stat().st_size / 1024, 1),
    }


# ============================================================
# Фильтр целевого размера + оценка портрета
# ============================================================

def parse_target_sizes(raw):
    sizes = []
    for part in raw.split(","):
        match = re.fullmatch(r"\s*([1-9]\d{0,4})\s*[xх×*]\s*([1-9]\d{0,4})\s*", part.lower())
        if not match:
            return []
        sizes.append(tuple(map(int, match.groups())))
    return sizes


def _load_target_sizes():
    """Целевые размеры портретов. По умолчанию 714x952.
    Можно переопределить настройкой БД photo_target_sizes = '714x952, 800x1067'."""
    raw = ""
    try:
        from . import db
        s = db.get_settings()
        raw = (s.get("photo_target_sizes") or "").strip()
    except Exception:
        raw = ""
    return parse_target_sizes(raw) or [(TARGET_W, TARGET_H)]


def is_portrait_target(m, sizes, tol):
    w = m.get("width", 0) or 0
    h = m.get("height", 0) or 0
    if w <= 0 or h <= 0:
        return False
    for tw, th in sizes:
        if abs(w - tw) <= tw * tol and abs(h - th) <= th * tol:
            return True
    return False


def _evaluate_target(m, reference):
    """Оценка ТОЛЬКО для целевых портретов: пропорцию не проверяем (она уже верна),
    проверяем заглушку, разрешение, лицо vs эталон."""
    issues = []
    if m.get("solid"):
        issues.append("Похоже на заглушку: однородный фон / силуэт")
    if m["width"] < DEFAULT_STANDARD["min_width"] or m["height"] < DEFAULT_STANDARD["min_height"]:
        issues.append("Низкое разрешение: " + str(m["width"]) + "x" + str(m["height"]))
    if reference:
        if m.get("face") and reference.get("face"):
            f, rf = m["face"], reference["face"]
            if abs(f["cx"] - rf["cx"]) > 0.15:
                issues.append("Лицо смещено по горизонтали относительно эталона")
            if abs(f["cy"] - rf["cy"]) > 0.20:
                issues.append("Лицо смещено по вертикали относительно эталона")
            if abs(f.get("fh", 0) - rf.get("fh", 0)) > 0.12:
                issues.append("Размер лица отличается от эталона")
        elif reference.get("face") and not m.get("face"):
            issues.append("В эталоне есть лицо, здесь лицо не найдено")
    else:
        if m.get("face"):
            f = m["face"]
            if not (DEFAULT_STANDARD["face_cx"][0] <= f["cx"] <= DEFAULT_STANDARD["face_cx"][1]):
                issues.append("Лицо смещено по горизонтали")
            if not (DEFAULT_STANDARD["face_cy"][0] <= f["cy"] <= DEFAULT_STANDARD["face_cy"][1]):
                issues.append("Лицо смещено по вертикали")
        else:
            issues.append("Лицо не найдено (проверьте вручную)")
    return issues


def evaluate(m, reference=None):
    """Совместимость со старыми вызовами (не используется photo_agent)."""
    issues = []
    if m.get("solid"):
        issues.append("Похоже на заглушку")
    return issues


# ============================================================
# Скачивание и фото-кэш (качаем ВСЁ, фильтруем на анализе)
# ============================================================

def download_image(src, dest):
    req = Request(src, headers={"User-Agent": UA})
    with urlopen(req, timeout=25) as r:
        data = r.read()
    if not data:
        raise ValueError("пустой ответ")
    if not any(data[:4].startswith(m) for m in IMAGE_MAGICS):
        raise ValueError("сервер вернул не изображение")
    dest.write_bytes(data)


def build_photo_cache(site_id, pages):
    if not PIL_OK:
        return {"error": "Pillow не установлен — фото-анализ недоступен"}
    img_dir = _img_dir(site_id)
    seen = set()
    tasks = []
    for p in pages:
        if p.get("error"):
            continue
        page_url = p.get("url", "")
        for img in p.get("images", []):
            src = img.get("src", "")
            if not src or src in seen:
                continue
            seen.add(src)
            tasks.append((page_url, src, img.get("alt", "")))
    metrics = load_metrics(site_id)
    manifest = []
    downloaded = 0
    failed = 0
    for page_url, src, alt in tasks:
        key = hashlib.md5(src.encode()).hexdigest()
        ext = Path(urlparse(src).path).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
            ext = ".jpg"
        file_path = img_dir / (key + ext)
        entry = {"src": src, "alt": alt, "page_url": page_url, "file": str(file_path)}
        if not file_path.exists() or file_path.stat().st_size == 0:
            try:
                download_image(src, file_path)
                downloaded += 1
            except Exception as e:
                entry["download_error"] = str(e)[:150]
                failed += 1
                manifest.append(entry)
                continue
        if key not in metrics:
            try:
                metrics[key] = deterministic_metrics(file_path)
            except Exception as e:
                entry["metric_error"] = str(e)[:150]
        manifest.append(entry)
    _metrics_path(site_id).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    _manifest_path(site_id).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"total": len(manifest), "downloaded": downloaded, "failed": failed}


# ============================================================
# Привязка страниц к таргет-разделам
# ============================================================

def _pages_for_target(target, pages, site_url):
    key = target.get("section_key", "")
    if key == "main":
        out = []
        for p in pages:
            u = p.get("url", "").rstrip("/")
            su = site_url.rstrip("/")
            if u == su or u == su + "/index" or u.endswith("/index.html"):
                out.append(p)
        if not out and pages:
            out = [pages[0]]
        return out
    kws = [k.strip().lower() for k in (target.get("page_keywords", "") or "").split(",") if k.strip()]
    if not kws:
        return []
    out = []
    for p in pages:
        if p.get("error"):
            continue
        hay = (p.get("url", "") + " " + (p.get("text", "") or "")[:3000]).lower()
        if any(k in hay for k in kws):
            out.append(p)
    return out


# ============================================================
# ФОТО-АГЕНТ: только целевые портреты 714x952
# ============================================================

def photo_agent(pages, site_type, site_url, site_id, targets):
    findings = []
    if not PIL_OK:
        return [{"agent": "photo", "severity": "INFO", "code": "NO_PIL",
                 "message": "Pillow не установлен — фото-анализ пропущен",
                 "url": site_url, "recommendation": "pip install Pillow"}]

    metrics = load_metrics(site_id)
    manifest = load_manifest(site_id)
    if not manifest:
        return findings

    reference = get_reference_info()
    sizes = _load_target_sizes()
    tol = TARGET_TOL

    # 1) все промеренные фото
    all_items = []
    for im in manifest:
        if im.get("download_error") or not im.get("file"):
            continue
        key = hashlib.md5(im["src"].encode()).hexdigest()
        m = metrics.get(key)
        if not m:
            continue
        all_items.append({"im": im, "m": m})

    # 2) ФИЛЬТР: оставляем только целевой размер 714x952
    target_items = []
    for x in all_items:
        if is_portrait_target(x["m"], sizes, tol):
            x["issues"] = _evaluate_target(x["m"], reference)
            target_items.append(x)
    non_target_skipped = len(all_items) - len(target_items)

    if not target_items:
        # портретов нужного размера нет — молчим, ничего не трогаем
        report = {
            "created_at": datetime.now().isoformat(),
            "reference_used": bool(reference),
            "target_sizes": sizes,
            "summary": {"total": 0, "bad": 0, "duplicate_pairs": 0,
                        "placeholders": 0, "non_target_skipped": non_target_skipped},
            "images": [],
        }
        _report_path(site_id).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return findings

    # 3) дубликаты — только среди целевых
    dup_pairs = 0
    for i in range(len(target_items)):
        for j in range(i + 1, len(target_items)):
            if hamming(target_items[i]["m"]["dhash"], target_items[j]["m"]["dhash"]) <= 6:
                target_items[i]["issues"].append("Дубликат другого портрета")
                target_items[j]["issues"].append("Дубликат другого портрета")
                dup_pairs += 1

    placeholders = [x for x in target_items if any("заглушку" in i.lower() for i in x["issues"])]

    # 4) агрегаты по разделам-таргетам (только целевые фото на страницах раздела)
    target_files = {x["im"]["file"] for x in target_items}
    for t in targets:
        t_pages = _pages_for_target(t, pages, site_url)
        if not t_pages:
            continue
        t_urls = {p.get("url", "") for p in t_pages}
        t_items = [x for x in target_items if x["im"].get("page_url") in t_urls]
        if not t_items:
            continue
        t_bad = [x for x in t_items if x["issues"]]
        label = t.get("section_label", t.get("section_key", ""))

        if t.get("require_faces"):
            staff_text = " ".join((p.get("text", "") or "") for p in t_pages)
            names = set(mm.group(0) for mm in FIO_RE.finditer(staff_text))
            photos = len(t_items)
            if names and photos < len(names) * 0.5:
                first_url = next(iter(t_urls), site_url)
                findings.append({"agent": "photo", "severity": "MEDIUM", "code": "STAFF_PHOTO_GAP",
                                 "message": "Раздел «" + label + "»: не у всех есть портрет 714x952 — ФИО " +
                                            str(len(names)) + ", портретов " + str(photos),
                                 "url": first_url,
                                 "recommendation": "Добавить фото в карточки сотрудников (портрет 714x952)."})

        if t_bad:
            sample = t_bad[0]["im"].get("page_url", site_url)
            findings.append({"agent": "photo", "severity": "MEDIUM", "code": "SECTION_PHOTO_ISSUES",
                             "message": "Раздел «" + label + "»: проблемных портретов " + str(len(t_bad)) +
                                        " из " + str(len(t_items)) + ". Первое: " +
                                        "; ".join(t_bad[0]["issues"])[:160],
                             "url": sample,
                             "recommendation": "Заменить портрет согласно стандарту (эталон в Правилах)."})

    if placeholders:
        findings.append({"agent": "photo", "severity": "HIGH", "code": "PHOTO_PLACEHOLDERS",
                         "message": "Портретов-заглушек (714x952): " + str(len(placeholders)) +
                                    " (однородный фон/силуэт)",
                         "url": placeholders[0]["im"].get("page_url", site_url),
                         "recommendation": "Убрать заглушки, заменить реальными портретами."})
    if dup_pairs:
        findings.append({"agent": "photo", "severity": "LOW", "code": "PHOTO_DUPLICATES",
                         "message": "Пар дубликатов портретов (714x952): " + str(dup_pairs),
                         "url": site_url,
                         "recommendation": "Удалить повторяющиеся портреты, оставить по одному."})

    # 5) отчёт: только целевые проблемные фото
    report = {
        "created_at": datetime.now().isoformat(),
        "reference_used": bool(reference),
        "target_sizes": sizes,
        "summary": {"total": len(target_items),
                    "bad": sum(1 for x in target_items if x["issues"]),
                    "duplicate_pairs": dup_pairs,
                    "placeholders": len(placeholders),
                    "non_target_skipped": non_target_skipped},
        "images": [{"file": Path(x["im"]["file"]).name, "src": x["im"]["src"],
                    "page_url": x["im"].get("page_url", ""),
                    "width": x["m"]["width"], "height": x["m"]["height"],
                    "ratio": x["m"]["ratio"], "face": x["m"]["face"],
                    "issues": x["issues"]} for x in target_items if x["issues"]],
    }
    _report_path(site_id).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return findings
