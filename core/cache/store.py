"""
core/cache/store.py — Two-tier knowledge cache.

Tier 1: knowledge packs (static JSON, loaded at startup, platform-matched)
Tier 2: learned.db (SQLite, written from LLM + execution results)

Lookup order: Tier 1 (packs) → Tier 2 (learned) → miss
Write order:  always Tier 2. Promotion to Tier 1 is manual via export.
"""
import json
import math
import re
import sqlite3
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Action verbs that change state (vs. just reading it) -- a query embedding
# is never allowed to semantic-match against a cached entry if the NEW query
# contains one of these, even if the phrases embed as very similar. Verified
# empirically that e.g. "turn off wifi" and "check wifi" embed at ~0.58
# cosine similarity (higher than some genuinely-same-topic pairs), so without
# this gate a toggle command could silently match a cached status-read entry
# and appear to succeed while doing nothing.
_SEMANTIC_ACTION_VERB_RE = re.compile(
    r"\b(set|turn on|turn off|enable|disable|change|toggle|connect|disconnect|"
    r"start|stop|open|close|launch|kill)\b",
    re.IGNORECASE,
)

_SEMANTIC_THRESHOLD = 0.55
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
    return _embed_model


def embed_text(text: str):
    import numpy as np
    return next(_get_embed_model().embed([text])).astype("float32")


def _cosine(a, b) -> float:
    import numpy as np
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


# ── Data shape ────────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    fingerprint: str
    action:      dict
    confidence:  float
    platform:    str
    hits:        int
    source:      str        # "pack" | "learned"
    intent_text: str = ""   # for fuzzy matching


# ── Fingerprinting ────────────────────────────────────────────────────────────

def fingerprint(text: str, platform: str = "any") -> str:
    key = f"{platform}::{text.lower().strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# ── Fuzzy matching helpers (zero external deps) ───────────────────────────────

def _ngrams(text: str, n: int = 3) -> set:
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    tokens = text.split()
    words = set(tokens)
    chars = set()
    for tok in tokens:
        for i in range(len(tok) - n + 1):
            chars.add(tok[i:i+n])
    return words | chars

def _similarity(a: str, b: str) -> float:
    sa, sb = _ngrams(a), _ngrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / math.sqrt(len(sa) * len(sb))


# ── SQLite schema ─────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned (
    fingerprint  TEXT PRIMARY KEY,
    intent_text  TEXT,
    action_json  TEXT NOT NULL,
    confidence   REAL DEFAULT 1.0,
    platform     TEXT DEFAULT 'any',
    hits         INTEGER DEFAULT 1,
    learned_at   INTEGER,
    last_used    INTEGER,
    embedding    BLOB
);
CREATE INDEX IF NOT EXISTS idx_platform  ON learned(platform);
CREATE INDEX IF NOT EXISTS idx_last_used ON learned(last_used);
"""


# ── Store ─────────────────────────────────────────────────────────────────────

class KnowledgeStore:

    def __init__(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parents[2]))
        import core.config as cfg
        self._cfg   = cfg
        self._packs: list[CacheEntry] = []    # Tier 1
        self._db:    sqlite3.Connection = None
        self._init_db()
        self._load_packs()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_db(self):
        cfg = self._cfg
        cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(cfg.DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        try:
            self._db.execute("ALTER TABLE learned ADD COLUMN embedding BLOB")
            self._db.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        self._db.commit()

    def _load_packs(self):
        """Load platform + shared knowledge packs into Tier 1."""
        cfg      = self._cfg
        resolver = self._try_get_resolver()
        targets  = [cfg.PLATFORM, "shared"]

        for target in targets:
            folder = cfg.PACK_DIR / target
            if not folder.exists():
                continue
            for pack_file in sorted(folder.glob("*.json")):
                try:
                    entries = json.loads(pack_file.read_text())
                except Exception as e:
                    print(f"[PACK] failed to load {pack_file}: {e}")
                    continue
                if resolver:
                    resolver.patch_pack(entries)
                for raw in entries:
                    intent = raw.get("intent", "")
                    platform = raw.get("platform", "any")
                    fp = fingerprint(intent, platform)
                    self._packs.append(CacheEntry(
                        fingerprint=fp,
                        action=raw["action"],
                        confidence=raw.get("confidence", 1.0),
                        platform=platform,
                        hits=0,
                        source="pack",
                        intent_text=intent,
                    ))

        print(f"[STORE] Loaded {len(self._packs)} pack entries")

    def _try_get_resolver(self):
        try:
            from platforms.android.resolver import AppResolver
            r = AppResolver()
            r.resolve()
            return r
        except Exception:
            return None

    # ── Lookup ────────────────────────────────────────────────────────────────

    def lookup(self, fp: str) -> Optional[CacheEntry]:
        """Exact fingerprint lookup. Tier 1 then Tier 2."""
        # Tier 1 — packs
        for entry in self._packs:
            if entry.fingerprint == fp:
                return entry

        # Tier 2 — learned
        row = self._db.execute(
            "SELECT * FROM learned WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if not row:
            return None

        # Staleness check
        stale = time.time() - self._cfg.STALE_DAYS * 86400
        if row["last_used"] < stale:
            return None

        if row["confidence"] < self._cfg.CACHE_THRESHOLD:
            return None

        self._db.execute(
            "UPDATE learned SET hits = hits+1, last_used = ? WHERE fingerprint = ?",
            (int(time.time()), fp),
        )
        self._db.commit()

        return CacheEntry(
            fingerprint=row["fingerprint"],
            action=json.loads(row["action_json"]),
            confidence=row["confidence"],
            platform=row["platform"],
            hits=row["hits"] + 1,
            source="learned",
            intent_text=row["intent_text"] or "",
        )

    def fuzzy_lookup(self, query: str) -> Optional[CacheEntry]:
        """N-gram cosine similarity over all entries. Returns best match above threshold."""
        cfg = self._cfg
        best_score = 0.0
        best_entry = None

        # Search Tier 1
        for entry in self._packs:
            if not entry.intent_text:
                continue
            score = _similarity(query, entry.intent_text)
            threshold = cfg.FUZZY_THRESHOLD_APP if entry.action.get("type") == "open_app" else cfg.FUZZY_THRESHOLD
            if score > best_score and score >= threshold:
                best_score = score
                best_entry = entry

        # Search Tier 2
        try:
            rows = self._db.execute("SELECT * FROM learned").fetchall()
            for row in rows:
                text = row["intent_text"] or ""
                if not text:
                    try:
                        aj = json.loads(row["action_json"])
                        text = aj.get("original_prompt", aj.get("description", ""))
                    except Exception:
                        continue
                score = _similarity(query, text)
                if score > best_score and score >= cfg.FUZZY_THRESHOLD:
                    best_score = score
                    best_entry = CacheEntry(
                        fingerprint=row["fingerprint"],
                        action=json.loads(row["action_json"]),
                        confidence=row["confidence"],
                        platform=row["platform"],
                        hits=row["hits"],
                        source="learned",
                        intent_text=text,
                    )
        except Exception:
            pass

        return best_entry

    # ── Write ─────────────────────────────────────────────────────────────────

    def learn(self, fp: str, intent_text: str, action: dict, platform: str = "any", confidence: float = 1.0):
        """Write or update a Tier 2 entry."""
        now = int(time.time())
        try:
            emb_bytes = embed_text(intent_text).tobytes() if intent_text else None
        except Exception as e:
            print(f"[EMBED] failed for {intent_text!r}: {e}")
            emb_bytes = None
        self._db.execute("""
            INSERT INTO learned (fingerprint, intent_text, action_json, confidence, platform, hits, learned_at, last_used, embedding)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                action_json = excluded.action_json,
                confidence  = excluded.confidence,
                hits        = hits + 1,
                last_used   = excluded.last_used,
                embedding   = COALESCE(excluded.embedding, learned.embedding)
        """, (fp, intent_text, json.dumps(action), confidence, platform, now, now, emb_bytes))
        self._db.commit()

    def semantic_lookup(self, query: str) -> Optional[CacheEntry]:
        """Embedding-similarity match across learned entries. Safety-gated:
        never matches if the query contains a state-changing verb (turn on/off,
        set, toggle, etc) since those must never be confused with a cached
        status-read entry, no matter how similar the phrasing embeds."""
        if _SEMANTIC_ACTION_VERB_RE.search(query):
            return None
        try:
            import numpy as np
            q_emb = embed_text(query)
        except Exception as e:
            print(f"[EMBED] query embed failed: {e}")
            return None

        best_score, best_row = 0.0, None
        rows = self._db.execute(
            "SELECT * FROM learned WHERE embedding IS NOT NULL"
        ).fetchall()
        for row in rows:
            cached_emb = np.frombuffer(row["embedding"], dtype="float32")
            score = _cosine(q_emb, cached_emb)
            if score > best_score:
                best_score, best_row = score, row

        if best_row is None or best_score < _SEMANTIC_THRESHOLD:
            return None

        print(f"[SEMANTIC] {query!r} → {best_row['intent_text']!r} (score={best_score:.3f})")
        self._db.execute(
            "UPDATE learned SET hits = hits+1, last_used = ? WHERE fingerprint = ?",
            (int(time.time()), best_row["fingerprint"]),
        )
        self._db.commit()
        return CacheEntry(
            fingerprint=best_row["fingerprint"],
            action=json.loads(best_row["action_json"]),
            confidence=best_row["confidence"],
            platform=best_row["platform"],
            hits=best_row["hits"] + 1,
            source="learned",
            intent_text=best_row["intent_text"] or "",
        )

    def evict(self, fp: str):
        """Remove a bad entry."""
        self._db.execute("DELETE FROM learned WHERE fingerprint = ?", (fp,))
        self._db.commit()

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        try:
            learned = self._db.execute("SELECT COUNT(*) FROM learned").fetchone()[0]
            top = self._db.execute(
                "SELECT intent_text, hits FROM learned ORDER BY hits DESC LIMIT 5"
            ).fetchall()
            return {
                "pack_entries":    len(self._packs),
                "learned_entries": learned,
                "top_hits":        [{"intent": r["intent_text"], "hits": r["hits"]} for r in top],
            }
        except Exception as e:
            return {"error": str(e)}
