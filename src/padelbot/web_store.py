"""Local platform state. Secrets and KUL state never appear in user/admin records."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

READ_JOB_KINDS = frozenset({'slots','account','quote','bookings'})
ACTIVE_JOB_STATUSES = ('queued','running','waiting_auth')

def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def google_return_path(value: str | None) -> str:
    """Only exact local availability and Discord server views survive login."""
    match = re.fullmatch(r"/#slots/([0-6])-([0-6])", value or "")
    if match and int(match[1]) <= int(match[2]):
        return value
    server = re.fullmatch(r"/#discord/server/([1-9][0-9]{0,18})", value or "")
    return value if server and int(server[1]) < 2**63 else "/"


class PlatformStore:
    def __init__(self, root: Path, owner_email: str):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "platform.sqlite3"
        with self.conn() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, google_sub TEXT UNIQUE, email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, owner INTEGER NOT NULL DEFAULT 0,
                    suspended INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    preferences TEXT NOT NULL DEFAULT '{}');
                CREATE UNIQUE INDEX IF NOT EXISTS single_owner ON users(owner) WHERE owner=1;
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth (
                    state TEXT PRIMARY KEY, browser TEXT NOT NULL, nonce TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires REAL NOT NULL,
                    return_to TEXT NOT NULL DEFAULT '/');
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT, created REAL NOT NULL, finished REAL, dedup TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL,
                    target TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS admin_tickets (
                    ticket TEXT PRIMARY KEY, session TEXT NOT NULL, expires REAL NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0);
            """)
            if "admin_until" not in {r[1] for r in c.execute("PRAGMA table_info(sessions)")}:
                c.execute("ALTER TABLE sessions ADD COLUMN admin_until REAL NOT NULL DEFAULT 0")
            if "return_to" not in {r[1] for r in c.execute("PRAGMA table_info(oauth)")}:
                c.execute("ALTER TABLE oauth ADD COLUMN return_to TEXT NOT NULL DEFAULT '/'")
            columns={r[1] for r in c.execute('PRAGMA table_info(users)')}
            if 'restricted' not in columns:
                c.execute('ALTER TABLE users ADD COLUMN restricted INTEGER NOT NULL DEFAULT 0')
            if 'avatar_url' not in columns:
                c.execute('ALTER TABLE users ADD COLUMN avatar_url TEXT')
            job_columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
            for field,declaration in [('started','REAL'),('auth_job_id','TEXT'),('reauth_attempts','INTEGER NOT NULL DEFAULT 0'),('auth_started','REAL')]:
                if field not in job_columns:c.execute(f'ALTER TABLE jobs ADD COLUMN {field} {declaration}')
            c.execute('CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,created)')
            owner=c.execute("SELECT email FROM users WHERE owner=1").fetchone()
            if owner and owner[0]!=owner_email.lower():
                raise ValueError("Wijzig de eigenaar uitsluitend via een expliciete profielmigratie.")
            c.execute("INSERT OR IGNORE INTO users(id,email,name,owner,created) VALUES(?,?,?,1,?)",
                      (uuid.uuid4().hex, owner_email.lower(), "Arnaud", time.time()))
        self.path.chmod(0o600)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def user(self, user_id: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def login_google(self, claims: dict) -> dict:
        # Identity is keyed by Google's immutable subject, never by a browser-supplied email.
        if claims.get("email_verified") is not True or not claims.get("sub") or not claims.get("email"):
            raise ValueError("Google heeft dit e-mailadres niet bevestigd.")
        email = claims["email"].lower()
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM users WHERE google_sub=?", (claims["sub"],)).fetchone()
            if row is None:
                existing = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if existing and existing["google_sub"] is not None:
                    raise ValueError("Dit profiel is aan een ander Google-account gekoppeld.")
                if existing:
                    c.execute("UPDATE users SET google_sub=? WHERE id=?", (claims["sub"], existing["id"]))
                    user_id = existing["id"]
                else:
                    user_id = uuid.uuid4().hex
                    c.execute("INSERT INTO users(id,google_sub,email,name,created) VALUES(?,?,?,?,?)",
                              (user_id, claims["sub"], email, str(claims.get("name") or email)[:120], time.time()))
            else:
                user_id = row["id"]
            from .identity import google_avatar
            c.execute('UPDATE users SET avatar_url=? WHERE id=?',(google_avatar(claims.get('picture')),user_id))
        user = self.user(user_id)
        if user["suspended"]:
            raise ValueError("Dit profiel is gepauzeerd. Neem contact op met de beheerder.")
        return user

    def new_session(self, user_id: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            c.execute("INSERT INTO sessions(token,user_id,csrf,expires) VALUES(?,?,?,?)", (digest(token), user_id, csrf, time.time()+43200))
        return token, csrf

    def session(self, token: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT user_id,csrf,admin_until FROM sessions WHERE token=? AND expires>?",
                            (digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def admin_ticket(self, token: str) -> str:
        ticket = secrets.token_urlsafe(40)
        with self.conn() as c:
            c.execute("DELETE FROM admin_tickets WHERE expires<?", (time.time(),))
            c.execute("INSERT INTO admin_tickets VALUES(?,?,?,0)", (digest(ticket),digest(token),time.time()+120))
        return ticket

    def verify_admin_ticket(self, ticket: str, email: str) -> bool:
        with self.conn() as c:
            row = c.execute("SELECT t.ticket FROM admin_tickets t JOIN sessions s ON s.token=t.session "
                            "JOIN users u ON u.id=s.user_id WHERE t.ticket=? AND t.expires>? AND s.expires>? "
                            "AND u.owner=1 AND u.suspended=0 AND u.email=?",
                            (digest(ticket),time.time(),time.time(),email.lower())).fetchone()
            if row:
                c.execute("UPDATE admin_tickets SET verified=1 WHERE ticket=?", (digest(ticket),))
        return bool(row)

    def activate_admin(self, ticket: str, token: str) -> bool:
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM admin_tickets WHERE ticket=? AND session=? AND verified=1 AND expires>?",
                            (digest(ticket),digest(token),time.time())).fetchone()
            if row:
                c.execute("DELETE FROM admin_tickets WHERE ticket=?", (digest(ticket),))
                c.execute("UPDATE sessions SET admin_until=? WHERE token=?", (time.time()+1800,digest(token)))
        return bool(row)

    def logout(self, token: str):
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (digest(token),))

    def oauth_start(self, return_to: str = "/") -> tuple[str, str, str, str]:
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        with self.conn() as c:
            c.execute("DELETE FROM oauth WHERE expires<?", (time.time(),))
            c.execute("INSERT INTO oauth(state,browser,nonce,verifier,expires,return_to) VALUES(?,?,?,?,?,?)",
                      (digest(state), digest(browser), nonce, verifier, time.time()+600, google_return_path(return_to)))
        return state, browser, nonce, verifier

    def oauth_consume(self, state: str, browser: str) -> dict | None:
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM oauth WHERE state=? AND browser=? AND expires>?",
                            (digest(state), digest(browser), time.time())).fetchone()
            if row:
                c.execute("DELETE FROM oauth WHERE state=?", (digest(state),))
        return dict(row) if row else None

    def preferences(self, user_id: str, values: dict | None = None) -> dict:
        if values is not None and values.get('automatic_payment'):
            self.assert_writable(user_id)
        with self.conn() as c:
            if values is not None:
                c.execute("UPDATE users SET preferences=? WHERE id=?", (json.dumps(values), user_id))
            row = c.execute("SELECT preferences FROM users WHERE id=?", (user_id,)).fetchone()
        return json.loads(row[0])

    def enqueue(self, user_id: str, kind: str, payload: dict, *, dedup: str | None = None) -> str:
        if kind in {'book','rule','community_join','draft_refresh'}:
            self.assert_writable(user_id)
        job_id = uuid.uuid4().hex
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            if dedup:
                row = c.execute("SELECT id FROM jobs WHERE dedup=?", (dedup,)).fetchone()
                if row:
                    return row[0]
            active = c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND status IN ('queued','running','waiting_auth')", (user_id,)).fetchone()[0]
            total = c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running','waiting_auth')").fetchone()[0]
            if active >= 3 or total >= 100:
                raise ValueError("Er staan al taken klaar. Wacht even tot die klaar zijn.")
            c.execute("INSERT INTO jobs(id,user_id,kind,payload,status,created,dedup) VALUES(?,?,?,?,'queued',?,?)",
                      (job_id, user_id, kind, json.dumps(payload), time.time(), dedup))
        return job_id

    def job(self, user_id: str, job_id: str) -> dict | None:
        with self.conn() as c:
            row = c.execute("SELECT id,kind,status,result,created,finished FROM jobs WHERE id=? AND user_id=?", (job_id,user_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def audit(self, actor: str, action: str, target: str | None = None):
        with self.conn() as c:
            c.execute("INSERT INTO audit(actor,action,target,created) VALUES(?,?,?,?)", (actor,action,target,time.time()))

    def suspend(self, actor: str, user_id: str, suspended: bool):
        return self.moderate(actor,user_id,suspended=suspended)

    def assert_writable(self,user_id: str):
        user=self.user(user_id)
        if not user or user['suspended']:
            raise ValueError('Dit profiel is gepauzeerd.')
        if user.get('restricted'):
            raise ValueError('Dit profiel heeft alleen leestoegang. Neem contact op met de beheerder.')
        return user

    def moderate(self,actor: str,user_id: str,*,suspended: bool | None=None,restricted: bool | None=None):
        with self.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
            if not row or row['owner']:
                raise ValueError('Dit profiel kan niet gemodereerd worden.')
            suspended=bool(row['suspended']) if suspended is None else suspended
            restricted=bool(row['restricted']) if restricted is None else restricted
            c.execute('UPDATE users SET suspended=?,restricted=? WHERE id=?',(int(suspended),int(restricted),user_id))
            if suspended or restricted:
                preferences=json.loads(row['preferences'])
                preferences['automatic_payment']=False
                c.execute('UPDATE users SET preferences=? WHERE id=?',(json.dumps(preferences),user_id))
                c.execute("UPDATE jobs SET status='cancelled',finished=? WHERE user_id=? AND status IN ('queued','waiting_auth') AND kind IN ('book','rule','community_join','draft_refresh')",(time.time(),user_id))
            if suspended:
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                c.execute("UPDATE jobs SET status='cancelled',finished=? WHERE user_id=? AND status IN ('queued','waiting_auth') AND kind!='draft_release'", (time.time(),user_id))
                tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'discord_oauth' in tables:c.execute('DELETE FROM discord_oauth WHERE user_id=?',(user_id,))
                if 'discord_deliveries' in tables:
                    c.execute("UPDATE discord_deliveries SET status='cancelled',payload='',finished=? WHERE user_id=? AND status='queued'",(time.time(),user_id))
        self.audit(actor,'moderation_updated',user_id)
        return self.user(user_id)
