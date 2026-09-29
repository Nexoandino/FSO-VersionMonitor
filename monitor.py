"""Monitor de versiones para páginas de release notes / readme (ManageEngine, Site24x7).
Avisa por Zoho Cliq cuando aparece un build nuevo.

  Ejecución normal :  python monitor.py
  Solo probar      :  python monitor.py --probar   (no guarda estado ni envía a Cliq)
  Panel web        :  se genera solo en cada ejecución -> abre index.html

Creado por Jadir Segura - Technical Consultant FSO, ManageEngine.

Necesita la variable de entorno CLIQ_WEBHOOK_URL (URL del canal/bot con zapikey).
"""
import json
import os
import re
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "productos.json")
STATE_FILE = os.path.join(BASE, "estado.json")
HIST_FILE = os.path.join(BASE, "historial.json")
PANEL_FILE = os.path.join(BASE, "index.html")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = rf"(?:{MONTHS})\s+\d{{1,2}},?\s*\d{{4}}"

# Formatos conocidos; se usa el primero que encuentre coincidencias.
DASH = "[-–—]"
PATTERNS = [
    # OpManager, OpUtils...:  "Build No 12.8.712- Sept 23, 2026" / "Build No: 12.9.134 — August 20, 2026"
    rf"Build\s*No\s*[.:]?\s*(?P<version>\d+(?:\.\d+)+)\s*{DASH}\s*(?P<date>{DATE})",
    # Applications Manager:   "New Features in Build 182400 - September 21, 2026"
    rf"in Build\s*(?P<version>\d+(?:\.\d+)*)\s*{DASH}\s*(?P<date>{DATE})",
    # Builds sin puntos:      "Build No: 128168 - February 27, 2024"
    rf"Build\s*(?:No)?\s*[.:]?\s*{DASH}?\s*(?P<version>\d{{3,}})\s*{DASH}\s*(?P<date>{DATE})",
    # Número simple sin prefijo: "### 6500 - September 17, 2026" (DDI Central)
    rf"(?<![\d.])(?P<version>\d{{4,}}(?:\.\d+)*)\s*{DASH}\s*(?P<date>{DATE})",
    # Respaldo genérico:      "Build 12.8.5 (July 1, 2026)" / "Version 1.2.3"
    rf"(?:Build|Version|Release)\s*(?:No\.?)?\s*[.:]?\s*v?(?P<version>\d+(?:\.\d+)+)(?:\s*[-–—(,]\s*(?P<date>{DATE}))?",
]
SECURITY = re.compile(r"security (?:fixes|issues)|CVE-\d{4}-\d+", re.I)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _version_of(m):
    return m.groupdict().get("version") or (m.group(1) if m.groups() else m.group(0))


def _key(version):
    return tuple(int(n) for n in re.findall(r"\d+", version))


def parse_builds(text, regex=None):
    """Devuelve el build MÁS ALTO de la página (no el primero: en algunas páginas
    los hotfix aparecen después de las secciones de 'New Features')."""
    patterns = [regex] if regex else PATTERNS
    matches = []
    for pat in patterns:
        matches = list(re.finditer(pat, text))
        if matches:
            break
    if not matches:
        pistas = re.findall(r"(?:Build|Version|Release)[^\n]{0,50}", text)[:3]
        raise ValueError(
            "No se encontró ninguna versión. Textos parecidos en la página: "
            + (" || ".join(pistas) if pistas else "(ninguno)")
        )

    best = max(_key(_version_of(m)) for m in matches)
    version = date = None
    security = False
    for i, m in enumerate(matches):
        v = _version_of(m)
        if _key(v) != best:
            continue
        version = version or v
        date = date or m.groupdict().get("date")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        security = security or bool(SECURITY.search(text[m.end():end]))
    return {"key": version, "date": date, "security": security}


def parse_novedades(text):
    """Páginas sin número de versión (Site24x7 'What's new'): la entrada más
    reciente es la primera fecha con año que aparece en la página."""
    dates = list(re.finditer(DATE, text))
    if not dates:
        raise ValueError("No se encontró ninguna fecha")
    first = dates[0]
    end = dates[1].start() if len(dates) > 1 else len(text)
    snippet = re.sub(r"\s+", " ", text[first.end():end]).strip()
    return {
        "key": f"{first.group(0)} | {snippet[:100]}",
        "date": first.group(0),
        "snippet": snippet[:200],
    }


def get_latest(product):
    r = requests.get(product["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    selector = product.get("selector")
    if selector:
        el = soup.select_one(selector)
        if el is None:
            raise ValueError(f"Selector no encontrado: {selector}")
        text = el.get_text(" ", strip=True)
    else:
        text = soup.get_text(" ", strip=True)

    if product.get("modo") == "novedades":
        return parse_novedades(text)
    return parse_builds(text, product.get("regex"))


def notify_cliq(message):
    """Envía a Cliq. Devuelve True si se entregó. Nunca imprime la URL (contiene la clave)."""
    webhook = os.environ.get("CLIQ_WEBHOOK_URL")
    if not webhook:
        print("[!] Falta CLIQ_WEBHOOK_URL, mensaje no enviado:\n" + message)
        return False
    try:
        r = requests.post(webhook, json={"text": message}, timeout=30)
    except requests.RequestException as e:
        print(f"[!] No se pudo conectar con Cliq ({type(e).__name__})")
        return False
    if r.status_code >= 400:
        print(f"[!] Cliq respondió {r.status_code}: {r.text[:300]}")
        print("    Revisa: nombre ÚNICO del canal (minúsculas), zapikey y dominio (cliq.zoho.com/.eu/.in).")
        return False
    return True


def build_message(p, info, previous):
    if p.get("modo") == "novedades":
        lines = [f"🆕 *Novedad en {p['name']}*", f"📅 {info['date']}", info["snippet"]]
    else:
        lines = [f"🆕 *Nueva versión de {p['name']}*", f"{previous} → *{info['key']}*"]
        if info.get("date"):
            lines.append(f"📅 {info['date']}")
        if info.get("security"):
            lines.append("🔒 Incluye correcciones de seguridad")
    lines.append(p["url"])
    panel = os.environ.get("PANEL_URL")  # enlace a la página principal del panel (opcional)
    if panel:
        lines.append(f"📊 Panel: {panel}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Panel web (index.html autocontenido)
# ---------------------------------------------------------------------------
TEMPLATE = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Monitor de versiones · FSO ManageEngine</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--tx:#151b28;--mut:#6b7385;--bd:#e5e8f0;--ok:#12a150;--warn:#d97706;--bad:#dc2626;--acc:#2f6bff;--sh:0 1px 2px rgba(16,24,40,.06),0 6px 18px rgba(16,24,40,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#141a2b;--tx:#e9edf7;--mut:#93a0b8;--bd:#232c44;--acc:#6f9bff;--sh:none}}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;padding-bottom:env(safe-area-inset-bottom,0)}
.hero{background:linear-gradient(135deg,#1e3a8a,#2f6bff 55%,#7c3aed);color:#fff;padding:calc(26px + env(safe-area-inset-top,0)) 16px 76px}
.wrap{max-width:1040px;margin:0 auto}
.hero .wrap{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap}
.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;opacity:.8}
h1{font-size:28px;margin:4px 0 2px}.hero p{margin:0;opacity:.85}
.pill{display:inline-flex;align-items:center;gap:8px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);padding:7px 14px;border-radius:99px;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:#cbd5e1}.dot.ok{background:#4ade80}.dot.bad{background:#f87171}
main{max-width:1040px;margin:-52px auto 0;padding:0 16px 30px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px;box-shadow:var(--sh);display:flex;gap:12px;align-items:center}
.kpi .ic{font-size:24px;width:44px;height:44px;border-radius:12px;background:var(--bg);display:grid;place-items:center}
.kpi b{display:block;font-size:24px;line-height:1.1}.kpi span{color:var(--mut);font-size:13px}
h2{font-size:13px;margin:30px 0 10px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bar input{flex:1;min-width:180px;padding:9px 12px;border-radius:10px;border:1px solid var(--bd);background:var(--card);color:var(--tx);font:inherit}
.chip{border:1px solid var(--bd);background:var(--card);color:var(--tx);padding:7px 13px;border-radius:99px;cursor:pointer;font:inherit;font-size:13px}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.pc{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px;box-shadow:var(--sh);border-top:3px solid var(--bd)}
.pc.new{border-top-color:var(--acc)}
.pt{display:flex;justify-content:space-between;gap:8px;align-items:center;font-weight:600}
.pt a{color:var(--tx);text-decoration:none}.pt a:hover{color:var(--acc)}
.v{font-size:28px;font-weight:700;margin:8px 0 6px;letter-spacing:-.02em}
.lbl{font-size:12px;color:var(--mut);margin-top:8px}.snip{margin:2px 0 8px;font-size:14px}
.meta{display:flex;flex-wrap:wrap;gap:4px 12px;color:var(--mut);font-size:12.5px;align-items:center}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:var(--bd);color:var(--tx)}
.badge.ok{background:var(--ok);color:#fff}.badge.bad{background:var(--bad);color:#fff}.badge.warn{background:#fef3c7;color:#92400e}.badge.new{background:var(--acc);color:#fff}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px;box-shadow:var(--sh)}
.strip{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px}
.strip i{width:14px;height:28px;border-radius:4px;background:var(--ok)}.strip i.warn{background:#f59e0b}.strip i.bad{background:var(--bad)}
.run{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--bd)}.run:last-child{border:0}
.err{flex-basis:100%;color:var(--bad);font-size:13px;padding-left:4px}
.tl{position:relative;margin-left:8px;padding-left:18px;border-left:2px solid var(--bd)}
.tl .it{position:relative;padding:0 0 14px}.tl .it:last-child{padding:0}
.tl .it:before{content:"";position:absolute;left:-25px;top:6px;width:10px;height:10px;border-radius:50%;background:var(--acc);border:2px solid var(--card)}
.tl small{display:block;color:var(--mut)}
.empty{color:var(--mut);margin:4px 0}
.creator{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.av{width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#2f6bff,#7c3aed);color:#fff;display:grid;place-items:center;font-weight:700;font-size:20px}
.creator b{font-size:17px;display:block}.creator span{color:var(--mut)}
footer{text-align:center;color:var(--mut);font-size:12.5px;padding:8px 16px 24px}
</style></head><body>
<div class="hero"><div class="wrap">
<div><div class="eyebrow">FSO · ManageEngine</div><h1>Monitor de versiones</h1><p id="sub"></p></div>
<div class="pill" id="pill"></div></div></div>
<main>
<section class="kpis" id="kpis"></section>
<h2>Últimas versiones</h2>
<div class="bar"><input id="q" type="search" placeholder="Buscar producto..."><span id="chips"></span></div>
<div class="grid" id="grid"></div>
<h2>Verificaciones diarias</h2>
<div class="card"><div class="strip" id="strip"></div><div id="runs"></div></div>
<h2>Cambios recientes</h2>
<div class="card" id="chg"></div>
<h2>Creador</h2>
<div class="card creator"><div class="av">JS</div>
<div><b>Jadir Segura</b><span>Technical Consultant · FSO</span><br><span>ManageEngine</span></div></div>
</main>
<footer>Monitor de versiones · creado por Jadir Segura, Technical Consultant FSO, ManageEngine</footer>
<script>
const D = __DATA__;
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x !== undefined) e.textContent = x; return e; };
const $ = id => document.getElementById(id);
const fmt = iso => iso ? new Date(iso).toLocaleString('es-CO', {dateStyle:'medium', timeStyle:'short'}) : '-';
const dias = iso => (Date.now() - new Date(iso)) / 864e5;
const runs = D.ejecuciones || [], prods = Object.entries(D.productos || {}), last = runs[0];
const problemas = r => r.errores + (r.avisos_fallidos || 0);
const esNuevo = p => (p.cambios || []).length > 0 && dias(p.cambios[0].detectado) < 7;
prods.sort((a, b) => (b[1].detectado || '').localeCompare(a[1].detectado || ''));

// Cabecera
const dot = el('i', 'dot'); let txt = 'Sin ejecuciones';
if (last) { txt = problemas(last) ? 'Última revisión con problemas' : 'Todo en orden'; dot.classList.add(problemas(last) ? 'bad' : 'ok'); }
$('pill').append(dot, txt);
$('sub').textContent = last ? 'Última revisión: ' + fmt(last.fecha) : 'Aún no hay datos';

// KPIs
const todos = prods.flatMap(([n, p]) => (p.cambios || []).map(c => ({n, ...c}))).sort((a, b) => b.detectado.localeCompare(a.detectado));
const rec = runs.slice(0, 30), okRuns = rec.filter(r => !problemas(r)).length;
[['📦', prods.length, 'Productos monitoreados'], ['🆕', todos.filter(c => dias(c.detectado) < 7).length, 'Cambios en 7 días'],
 ['✅', rec.length ? Math.round(100 * okRuns / rec.length) + '%' : '-', 'Revisiones sin problemas'], ['🕒', runs.length, 'Revisiones registradas']]
 .forEach(([i, v, l]) => { const k = el('div', 'kpi'), d = el('div'); d.append(el('b', '', v), el('span', '', l)); k.append(el('div', 'ic', i), d); $('kpis').append(k); });

// Tarjetas con búsqueda y filtros
let q = '', f = 'todos';
function pintar() {
  const g = $('grid'); g.replaceChildren();
  const lista = prods.filter(([n, p]) => n.toLowerCase().includes(q) &&
    (f === 'todos' || (f === 'nuevos' && esNuevo(p)) || (f === 'seguridad' && p.seguridad)));
  lista.forEach(([n, p]) => {
    const c = el('article', 'pc' + (esNuevo(p) ? ' new' : '')), h = el('div', 'pt'), a = el('a', '', n);
    a.href = p.url; a.target = '_blank'; a.rel = 'noopener'; h.append(a);
    if (esNuevo(p)) h.append(el('span', 'badge new', 'nuevo'));
    c.append(h);
    if (p.modo === 'novedades') c.append(el('div', 'lbl', 'Última novedad'), el('div', 'snip', (p.snippet || '').slice(0, 120)));
    else c.append(el('div', 'v', p.version));
    const m = el('div', 'meta');
    m.append(el('span', '', (p.modo === 'novedades' ? 'Fecha: ' : 'Publicada: ') + (p.fecha || '-')), el('span', '', 'Detectada: ' + fmt(p.detectado)));
    if (p.seguridad) m.append(el('span', 'badge warn', '🔒 seguridad'));
    c.append(m); g.append(c);
  });
  if (!lista.length) g.append(el('p', 'empty', 'Sin resultados'));
}
[['todos', 'Todos'], ['nuevos', 'Nuevos'], ['seguridad', 'Seguridad']].forEach(([k, l]) => {
  const b = el('button', 'chip' + (k === f ? ' on' : ''), l); b.dataset.k = k;
  b.onclick = () => { f = k; document.querySelectorAll('.chip').forEach(x => x.classList.toggle('on', x.dataset.k === k)); pintar(); };
  $('chips').append(b, ' ');
});
$('q').oninput = e => { q = e.target.value.toLowerCase(); pintar(); };
pintar();

// Verificaciones diarias
rec.slice().reverse().forEach(r => {
  const i = el('i', problemas(r) ? (r.ok ? 'warn' : 'bad') : '');
  i.title = fmt(r.fecha) + ' - ' + r.ok + ' ok, ' + problemas(r) + ' con problemas, ' + r.cambios + ' cambios'; $('strip').append(i);
});
runs.slice(0, 7).forEach(r => {
  const d = el('div', 'run'); d.append(el('b', '', fmt(r.fecha)), el('span', 'badge ' + (problemas(r) ? 'bad' : 'ok'), problemas(r) ? problemas(r) + ' con problemas' : 'Sin problemas'),
    el('span', 'badge', r.ok + ' páginas ok'));
  if (r.cambios) d.append(el('span', 'badge new', r.cambios + ' cambio(s)'));
  (r.detalle || []).filter(x => x.estado !== 'ok').forEach(x => d.append(el('div', 'err', x.nombre + ': ' + x.mensaje)));
  $('runs').append(d);
});
if (!runs.length) $('runs').append(el('p', 'empty', 'Sin ejecuciones todavía'));

// Cambios recientes
if (todos.length) { const t = el('div', 'tl'); todos.slice(0, 15).forEach(c => {
  const d = el('div', 'it'), b = el('b', '', c.n + '  '); d.append(b, el('span', '', c.de + ' → ' + c.a));
  if (c.seguridad) d.append(' ', el('span', 'badge warn', '🔒 seguridad'));
  d.append(el('small', '', 'Detectado ' + fmt(c.detectado) + (c.fecha ? ' · publicada ' + c.fecha : ''))); t.append(d); });
  $('chg').append(t);
} else $('chg').append(el('p', 'empty', 'Aún no se ha detectado ningún cambio desde que empezó el monitoreo.'));
</script></body></html>
"""


def generar(hist, salida=PANEL_FILE):
    datos = json.dumps(hist, ensure_ascii=False).replace("</", "<\\/")
    with open(salida, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.replace("__DATA__", datos))


def registrar(hist, p, info, ahora):
    """Guarda la última versión vista y los cambios. Devuelve True si hubo cambio."""
    prod = hist["productos"].setdefault(p["name"], {"cambios": []})
    cambio = prod.get("version") not in (None, info["key"])
    if cambio:
        prod["cambios"].insert(0, {
            "de": prod["version"], "a": info["key"], "fecha": info.get("date"),
            "detectado": ahora, "seguridad": bool(info.get("security")),
        })
        del prod["cambios"][20:]
    if cambio or "version" not in prod:
        prod["detectado"] = ahora
    prod.update(version=info["key"], fecha=info.get("date"), seguridad=bool(info.get("security")),
                url=p["url"], snippet=info.get("snippet"), modo=p.get("modo"))
    return cambio


def main():
    probar = "--probar" in sys.argv
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f"[x] No encuentro productos.json. Debe estar junto a monitor.py:\n    {CONFIG_FILE}")
    try:
        products = load_json(CONFIG_FILE, [])
    except json.JSONDecodeError as e:
        sys.exit(f"[x] productos.json tiene un error de formato (línea {e.lineno}): {e.msg}")
    if not products:
        sys.exit("[x] productos.json está vacío: no hay páginas que revisar.")
    print(f"Carpeta de trabajo: {BASE}\nRevisando {len(products)} productos...")
    state = load_json(STATE_FILE, {})
    hist = load_json(HIST_FILE, {"productos": {}, "ejecuciones": []})
    for p in products:  # si un producto cambió de nombre ("antes"), conserva su historial
        antes = p.get("antes")
        if antes and antes in state and p["name"] not in state:
            state[p["name"]] = state.pop(antes)
        if antes and antes in hist["productos"] and p["name"] not in hist["productos"]:
            hist["productos"][p["name"]] = hist["productos"].pop(antes)
    nombres = {p["name"] for p in products}
    hist["productos"] = {k: v for k, v in hist["productos"].items() if k in nombres}
    ahora = datetime.now().astimezone().isoformat(timespec="minutes")
    errors, cliq_failed, detalle, cambios = [], [], [], 0

    for p in products:
        name = p["name"]
        try:
            info = get_latest(p)
        except Exception as e:  # una página caída no debe frenar las demás
            print(f"[x] {name}: {e}")
            errors.append(f"{name}: {e}")
            detalle.append({"nombre": name, "estado": "error", "mensaje": str(e)[:200]})
            continue

        latest = info["key"]
        if probar:
            extra = f" | {info['date']}" if info.get("date") and info["date"] not in latest else ""
            extra += " | 🔒 seguridad" if info.get("security") else ""
            print(f"[ok] {name}: {latest}{extra}")
            continue

        previous = state.get(name)
        avisado = True
        if previous is None:
            print(f"[+] {name}: primera lectura, {latest} (sin aviso)")
        elif latest != previous:
            print(f"[!] {name}: {previous} -> {latest}")
            if not notify_cliq(build_message(p, info, previous)):
                cliq_failed.append(name)  # no se guarda el estado: se reintenta en la próxima ejecución
                avisado = False
        else:
            print(f"[=] {name}: sigue en {latest}")

        cambios += registrar(hist, p, info, ahora)
        if avisado:
            state[name] = latest
        detalle.append({
            "nombre": name, "estado": "ok" if avisado else "aviso_fallido",
            "mensaje": latest if avisado else f"versión nueva {latest} detectada, pero no se pudo avisar a Cliq",
        })

    if not probar:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        hist["ejecuciones"].insert(0, {
            "fecha": ahora, "ok": sum(d["estado"] == "ok" for d in detalle), "errores": len(errors),
            "avisos_fallidos": len(cliq_failed), "cambios": cambios, "detalle": detalle,
        })
        del hist["ejecuciones"][60:]
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, indent=2, ensure_ascii=False)
        generar(hist)
        if errors:
            notify_cliq("⚠️ Monitor de versiones con errores:\n" + "\n".join(errors))
        if cliq_failed:
            print("[!] Versiones nuevas SIN avisar (fallo de Cliq): " + ", ".join(cliq_failed))

    if errors or cliq_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
