#!/usr/bin/env python3
"""Vinted Pokémon card monitor with Telegram alerts and Claude Vision analysis."""

import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

# os.environ (Railway, variables système) est lu en priorité.
# load_dotenv() ne remplace jamais une variable déjà présente dans l'environnement —
# elle sert uniquement en développement local quand le fichier .env existe.
load_dotenv(override=False)

# ── Configuration ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

if not TELEGRAM_TOKEN or not CHAT_ID:
    sys.exit(
        "Erreur : TELEGRAM_TOKEN et CHAT_ID sont introuvables.\n"
        "  - En local : définissez-les dans le fichier .env\n"
        "  - Sur Railway : ajoutez-les dans Settings > Variables"
    )

CHECK_INTERVAL = 180  # 3 minutes
CLAUDE_MODEL = "claude-opus-4-7"

SEEN_IDS_FILE = Path("seen_ids.json")
KEYWORDS_FILE = Path("keywords.txt")

VINTED_BASE_URL = "https://www.vinted.fr"
VINTED_API_URL = f"{VINTED_BASE_URL}/api/v2/catalog/items"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.vinted.fr/",
    "Origin": "https://www.vinted.fr",
    "DNT": "1",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# ── Prompt d'analyse PSA (mis en cache côté Claude) ───────────────────────────
ANALYSIS_SYSTEM = """\
Tu es un expert en gradation de cartes Pokémon PSA avec 10 ans d'expérience.

Lorsqu'on te présente des photos d'une annonce Vinted pour une carte Pokémon :

1. Identifie quelle image montre la FACE AVANT (artwork, nom de la carte, PV visibles)
   et quelle image montre la FACE ARRIÈRE (motif Poké Ball standard rouge/bleu).

2. Fournis ton analyse dans ce format EXACT (conserve les en-têtes tels quels) :

--- FACE AVANT ---

CENTRAGE :
- Haut/Bas : X%/Y%
- Gauche/Droite : X%/Y%
- Confiance : [Haute/Moyenne/Faible]

COINS (analyser les 4 coins individuellement) :
- Coin haut gauche : [Parfait/Léger usure/Usure visible/Touché]
- Coin haut droit : [Parfait/Léger usure/Usure visible/Touché]
- Coin bas gauche : [Parfait/Léger usure/Usure visible/Touché]
- Coin bas droit : [Parfait/Léger usure/Usure visible/Touché]

BORDS (edges) :
- Silvering visible : [Oui/Non/Léger]
- État général des bords : [Parfait/Bon/Moyen/Mauvais]

SURFACE :
- Rayures sur la carte (ignorer top-loader et protection plastique) : [Oui/Non/Légères]
- Indentations ou enfoncements : [Oui/Non]
- Traces de doigts : [Oui/Non]
- Défauts d'impression usine : [Oui/Non - préciser si oui]
- Points blancs (whitening) : [Oui/Non/Légers]

ÉTAT DU TOP-LOADER :
- Jauni : [Oui/Non]
- Très rayé : [Oui/Non]
- Indice conservation : [Bonne/Moyenne/Mauvaise]

NOTE PSA ESTIMÉE : [10/9/8/7 ou moins]
SCORE DE PRIORITÉ : [1-10]
JUSTIFICATION : [deux lignes max]

--- FACE ARRIÈRE ---

[Si le dos de la carte est visible dans une des photos :]
CENTRAGE :
- Haut/Bas : X%/Y%
- Gauche/Droite : X%/Y%
- Confiance : [Haute/Moyenne/Faible]

[Si le dos n'est pas visible :]
NON DISPONIBLE
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Persistence ───────────────────────────────────────────────────────────────
def load_seen_ids() -> set:
    if SEEN_IDS_FILE.exists():
        return set(json.loads(SEEN_IDS_FILE.read_text()))
    return set()


def save_seen_ids(ids: set) -> None:
    SEEN_IDS_FILE.write_text(json.dumps(list(ids)))


def load_keywords() -> list[str]:
    if not KEYWORDS_FILE.exists():
        log.error(f"Fichier {KEYWORDS_FILE} introuvable.")
        return []
    return [
        line.strip()
        for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ── Vinted scraping ───────────────────────────────────────────────────────────
class VintedScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._refresh_session()

    def _refresh_session(self):
        """Visit Vinted search page to obtain fresh session cookies."""
        try:
            self.session.get(VINTED_BASE_URL, timeout=10)
            time.sleep(random.uniform(1.0, 2.5))
            self.session.get(
                f"{VINTED_BASE_URL}/catalog",
                params={"search_text": "pokemon"},
                timeout=10,
            )
            log.info("Session Vinted initialisée.")
        except Exception as e:
            log.warning(f"Impossible d'initialiser la session : {e}")

    def search(self, keyword: str) -> list[dict]:
        time.sleep(random.uniform(2.0, 5.0))
        params = {
            "search_text": keyword,
            "per_page": 20,
            "page": 1,
            "order": "newest_first",
        }
        for attempt in range(2):
            try:
                resp = self.session.get(VINTED_API_URL, params=params, timeout=15)
                resp.raise_for_status()

                if not resp.content:
                    log.warning(f"Réponse vide pour « {keyword} » (tentative {attempt + 1}/2)")
                    self._refresh_session()
                    time.sleep(random.uniform(4.0, 8.0))
                    continue

                content_type = resp.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    log.warning(
                        f"Vinted a renvoyé du HTML pour « {keyword} » "
                        f"(bot détecté ?), renouvellement de session…"
                    )
                    self._refresh_session()
                    time.sleep(random.uniform(8.0, 15.0))
                    continue

                return resp.json().get("items", [])

            except json.JSONDecodeError:
                preview = resp.text[:300] if resp.text else "(vide)"
                log.warning(
                    f"JSON invalide pour « {keyword} » (tentative {attempt + 1}/2). "
                    f"Début de la réponse : {preview!r}"
                )
                self._refresh_session()
                time.sleep(random.uniform(6.0, 12.0))

            except requests.HTTPError as e:
                status = e.response.status_code if e.response else "?"
                if status in (401, 403):
                    log.warning(f"Session rejetée (HTTP {status}), renouvellement…")
                    self._refresh_session()
                    time.sleep(random.uniform(4.0, 8.0))
                else:
                    log.error(f"Erreur HTTP {status} pour « {keyword} »")
                    break

            except Exception as e:
                log.error(f"Erreur inattendue pour « {keyword} » : {e}")
                break

        return []

    def fetch_item_photos(self, item_id: str) -> list[str]:
        """Fetch all photo URLs for a given item from the full item endpoint."""
        try:
            time.sleep(random.uniform(0.5, 1.5))
            resp = self.session.get(
                f"{VINTED_BASE_URL}/api/v2/items/{item_id}",
                timeout=15,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type or not resp.content:
                return []

            data = resp.json()
            item_data = data.get("item", data)
            photos = item_data.get("photos", [])

            urls = []
            for photo in photos:
                url = (
                    photo.get("full_size_url")
                    or photo.get("url")
                    or photo.get("image_url")
                )
                if url and url.startswith("http"):
                    urls.append(url)
            return urls

        except Exception as e:
            log.warning(f"Impossible de récupérer les photos pour l'item {item_id} : {e}")
            return []


# ── Claude Vision ─────────────────────────────────────────────────────────────
class CardAnalyzer:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def analyze(self, photo_urls: list[str]) -> dict | None:
        if not photo_urls:
            return None

        # Build content: images (URL type) + analysis request
        content = []
        for url in photo_urls[:4]:
            content.append({
                "type": "image",
                "source": {"type": "url", "url": url},
            })
        content.append({
            "type": "text",
            "text": (
                "Identifie la face avant et la face arrière parmi ces images, "
                "puis effectue l'analyse complète selon tes instructions."
            ),
        })

        try:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                # Prompt caching sur le system prompt long et constant
                system=[{
                    "type": "text",
                    "text": ANALYSIS_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": content}],
            )
            raw_text = response.content[0].text
            result = _parse_analysis(raw_text)
            cache_read = response.usage.cache_read_input_tokens or 0
            log.info(
                f"  Analyse Claude : score {result.get('priority_score', '?')}/10 "
                f"| PSA {result.get('psa_note', '?')} "
                f"| cache_read={cache_read}"
            )
            return result
        except Exception as e:
            log.error(f"Erreur Claude Vision : {e}")
            return None


def _parse_analysis(text: str) -> dict:
    """Extract structured fields from Claude's grading response."""

    def find(pattern: str, src: str, default: str = "—") -> str:
        m = re.search(pattern, src, re.IGNORECASE)
        return m.group(1).strip() if m else default

    # Split into front / back sections
    front_text = text
    back_text = ""
    if "--- FACE AVANT ---" in text:
        parts = text.split("--- FACE AVANT ---", 1)
        after = parts[1] if len(parts) > 1 else ""
        if "--- FACE ARRIÈRE ---" in after:
            sections = after.split("--- FACE ARRIÈRE ---", 1)
            front_text = sections[0]
            back_text = sections[1]
        else:
            front_text = after

    back_available = bool(back_text.strip()) and "NON DISPONIBLE" not in back_text.upper()

    result = {
        "centering_hb":        find(r"Haut/Bas\s*:\s*([^\n]+)", front_text),
        "centering_gd":        find(r"Gauche/Droite\s*:\s*([^\n]+)", front_text),
        "centering_conf":      find(r"Confiance\s*:\s*([^\n]+)", front_text),
        "corner_tl":           find(r"Coin haut gauche\s*:\s*([^\n]+)", front_text),
        "corner_tr":           find(r"Coin haut droit\s*:\s*([^\n]+)", front_text),
        "corner_bl":           find(r"Coin bas gauche\s*:\s*([^\n]+)", front_text),
        "corner_br":           find(r"Coin bas droit\s*:\s*([^\n]+)", front_text),
        "silvering":           find(r"Silvering visible\s*:\s*([^\n]+)", front_text),
        "edges":               find(r"État général des bords\s*:\s*([^\n]+)", front_text),
        "scratches":           find(r"Rayures sur la carte[^:\n]*:\s*([^\n]+)", front_text),
        "indentations":        find(r"Indentations[^:\n]*:\s*([^\n]+)", front_text),
        "fingerprints":        find(r"Traces de doigts\s*:\s*([^\n]+)", front_text),
        "print_defects":       find(r"Défauts d.impression[^:\n]*:\s*([^\n]+)", front_text),
        "whitening":           find(r"Points blancs[^:\n]*:\s*([^\n]+)", front_text),
        "toploader_yellowed":  find(r"Jauni\s*:\s*([^\n]+)", front_text),
        "toploader_scratched": find(r"Très rayé\s*:\s*([^\n]+)", front_text),
        "toploader_conservation": find(r"Indice conservation\s*:\s*([^\n]+)", front_text),
        "psa_note":            find(r"NOTE PSA ESTIMÉE\s*:\s*([^\n]+)", front_text),
        "priority_score_raw":  find(r"SCORE DE PRIORITÉ\s*:\s*([^\n]+)", front_text),
        "justification":       find(r"JUSTIFICATION\s*:\s*([^\n]+)", front_text),
        "back_available":      back_available,
        "back_centering_hb":   find(r"Haut/Bas\s*:\s*([^\n]+)", back_text) if back_available else "—",
        "back_centering_gd":   find(r"Gauche/Droite\s*:\s*([^\n]+)", back_text) if back_available else "—",
    }

    try:
        m = re.search(r"\d+", result["priority_score_raw"])
        result["priority_score"] = int(m.group()) if m else 5
    except Exception:
        result["priority_score"] = 5

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_seller_reliability(user: dict) -> str:
    try:
        rating = float(user.get("feedback_reputation") or 0)
        count = int(
            user.get("positive_feedback_count")
            or user.get("feedback_count")
            or 0
        )
    except (TypeError, ValueError):
        return "⚠️ Peu d'historique"

    if count < 5:
        return "⚠️ Peu d'historique"
    if rating < 3.5:
        return "🚨 Méfiance"
    return "⭐ Fiable"


def get_listing_age(item: dict) -> tuple[str, bool]:
    """Return (human-readable age string, can_negotiate bool)."""
    try:
        ts = item.get("created_at_ts")
        if ts:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        else:
            raw = item.get("created_at", "")
            if not raw:
                return "—", False
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

        delta = datetime.now(timezone.utc) - dt
        hours = delta.total_seconds() / 3600

        if hours < 1:
            age_str = f"{int(delta.total_seconds() / 60)} min"
        elif hours < 24:
            age_str = f"{int(hours)}h"
        else:
            age_str = f"{int(hours / 24)}j"

        return age_str, hours > 24
    except Exception:
        return "—", False


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")
        return False


def parse_price(item: dict) -> str:
    price = item.get("price")
    if isinstance(price, dict):
        amount = price.get("amount", "?")
        currency = price.get("currency_code", "€")
    else:
        amount = price or "?"
        currency = item.get("currency", "€")
    return f"{amount} {currency}"


def parse_url(item: dict) -> str:
    url = item.get("url", "")
    if url and not url.startswith("http"):
        return f"{VINTED_BASE_URL}{url}"
    return url or f"{VINTED_BASE_URL}/items/{item.get('id', '')}"


def format_basic_message(item: dict, keyword: str) -> str:
    """Fallback message when Claude Vision is unavailable."""
    title = item.get("title", "—")
    price_str = parse_price(item)
    status = item.get("status", "—")
    url = parse_url(item)
    return (
        f"🃏 <b>{title}</b>\n"
        f"💰 Prix : <b>{price_str}</b>\n"
        f"✨ État : {status}\n"
        f"🔍 Recherche : <i>{keyword}</i>\n"
        f'🔗 <a href="{url}">Voir l\'annonce</a>'
    )


def format_vision_message(item: dict, analysis: dict, keyword: str) -> str:
    """Full message with Claude Vision analysis."""
    title = item.get("title", "—")
    price_str = parse_price(item)
    url = parse_url(item)
    user = item.get("user", {})

    age_str, can_negotiate = get_listing_age(item)
    rating = user.get("feedback_reputation", "?")
    sales_count = (
        user.get("positive_feedback_count")
        or user.get("feedback_count")
        or "?"
    )
    reliability = get_seller_reliability(user)

    score = analysis.get("priority_score", 5)
    score_emoji = "🔥" if score >= 8 else ("⚡" if score >= 6 else "😐")

    defects = []
    for key, label in [
        ("scratches",    "Rayures"),
        ("indentations", "Indentations"),
        ("fingerprints", "Traces de doigts"),
        ("print_defects","Défauts impression"),
        ("whitening",    "Whitening"),
    ]:
        val = analysis.get(key, "Non")
        if val and val.strip().lower() not in ("non", "—", ""):
            defects.append(f"{label}: {val}")
    surface_str = " | ".join(defects) if defects else "Aucun défaut visible"

    age_line = f"⏱️ {age_str}"
    if can_negotiate:
        age_line += " — 💡 Annonce ancienne - négociation possible"

    if analysis.get("back_available"):
        back_section = (
            f"📐 CENTRAGE DOS :\n"
            f"Haut/Bas: {analysis.get('back_centering_hb','—')} | "
            f"Gauche/Droite: {analysis.get('back_centering_gd','—')}"
        )
    else:
        back_section = "📐 CENTRAGE DOS :\nDos de la carte non disponible"

    return (
        f"[{score}/10] {score_emoji} <b>{title}</b> — {price_str}\n"
        f"👤 Vendeur : {rating}⭐ ({sales_count} ventes) — {reliability}\n"
        f"{age_line}\n"
        f"\n"
        f"📐 CENTRAGE FACE :\n"
        f"Haut/Bas: {analysis.get('centering_hb','—')} | "
        f"Gauche/Droite: {analysis.get('centering_gd','—')}\n"
        f"Confiance: {analysis.get('centering_conf','—')}\n"
        f"\n"
        f"{back_section}\n"
        f"\n"
        f"📐 COINS :\n"
        f"↖️ {analysis.get('corner_tl','—')} | ↗️ {analysis.get('corner_tr','—')}\n"
        f"↙️ {analysis.get('corner_bl','—')} | ↘️ {analysis.get('corner_br','—')}\n"
        f"\n"
        f"📐 BORDS :\n"
        f"Silvering: {analysis.get('silvering','—')} | "
        f"Bords: {analysis.get('edges','—')}\n"
        f"\n"
        f"🔍 SURFACE :\n"
        f"{surface_str}\n"
        f"\n"
        f"📦 TOP-LOADER : {analysis.get('toploader_conservation','—')}\n"
        f"\n"
        f"⭐ NOTE PSA ESTIMÉE : {analysis.get('psa_note','—')}\n"
        f"🎯 SCORE OPPORTUNITÉ : {score}/10\n"
        f"📝 {analysis.get('justification','—')}\n"
        f"\n"
        f'🔗 <a href="{url}">Voir l\'annonce</a>'
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("=== Pokémon Vinted Alert Bot démarré ===")

    scraper = VintedScraper()
    seen_ids = load_seen_ids()

    analyzer: CardAnalyzer | None = None
    if ANTHROPIC_API_KEY:
        analyzer = CardAnalyzer(ANTHROPIC_API_KEY)
        log.info(f"Claude Vision activé (modèle : {CLAUDE_MODEL}).")
    else:
        log.warning(
            "ANTHROPIC_API_KEY non définie — analyse visuelle désactivée. "
            "Ajoutez-la dans .env ou dans les variables Railway."
        )

    send_telegram("✅ <b>Bot Pokémon Vinted démarré !</b>\nSurveillance en cours…")

    while True:
        keywords = load_keywords()
        if not keywords:
            log.warning("Aucun mot-clé trouvé dans keywords.txt, nouvelle tentative dans 60s.")
            time.sleep(60)
            continue

        log.info(f"Vérification de {len(keywords)} mot(s)-clé(s)…")
        new_count = 0

        for keyword in keywords:
            items = scraper.search(keyword)
            for item in items:
                item_id = str(item.get("id", ""))
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                # Vision analysis (optional)
                analysis = None
                if analyzer:
                    photo_urls = scraper.fetch_item_photos(item_id)
                    if not photo_urls:
                        # Fallback: use the main photo from search results
                        main_photo = (item.get("photo") or {}).get("url")
                        if main_photo:
                            photo_urls = [main_photo]
                    if photo_urls:
                        analysis = analyzer.analyze(photo_urls)

                if analysis:
                    msg = format_vision_message(item, analysis, keyword)
                else:
                    msg = format_basic_message(item, keyword)

                if send_telegram(msg):
                    log.info(f"  → Alerte : {item.get('title', item_id)}")
                    new_count += 1
                time.sleep(0.5)

        save_seen_ids(seen_ids)
        log.info(
            f"{new_count} nouvelle(s) annonce(s). "
            f"Prochaine vérif dans {CHECK_INTERVAL // 60} min."
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
