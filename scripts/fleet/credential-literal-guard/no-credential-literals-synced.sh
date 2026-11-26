#!/usr/bin/env bash
# no-credential-literals-synced.sh - lock: no non-Supabase credential literal may live in a Syncthing-synced folder
# on this host, including each folder's .stversions and ~/.claude session transcripts.
#   COP-522 (2026-09-13): sk-ant/sk-proj/sk-svcacct/sk-or/sk-, glpat-, AIza, AKIA (+ adjacent AWS secret), xox?-, gh?_, github_pat_
#   COP-526 (2026-09-13): Stripe sk_/rk_live_ + whsec_, Telegram bot tokens, Discord bot tokens (snowflake-checked),
#     figd_, DeepSeek-style sk-<32hex>, sk-ant-oat01/ort01, Vercel vcp_/vci_/vca_/vcr_/vck_ (+ 24-char legacy only after a
#     VERCEL*TOKEN/KEY/SECRET name), re_ (Resend), Twilio SK/AC + auth token after a TWILIO*TOKEN/SECRET name,
#     Mailchimp <32hex>-usN, n8n API JWTs (iss=n8n), phx_, lin_api_, ntn_ / secret_<43>, PEM private-key blocks
# Companion of no-discord-webhook-literals.sh (COP-513/514) and uses the same --synced directory set.
# Detection core is a verbatim copy of ~/Sandbox/scripts/cop526_redact.py (placeholders, S3 presigned-URL key ids,
# and hash+location pinned fixtures are not failures). Canonical stores (.env*, ~/.claude/.credentials.json) are skipped.
# COP-552: a proven-fake value is also not a failure ANYWHERE when its full sha256+shape is a row in
# ~/.fleet/credential-literal-guard/known-fake-hashes.tsv (override: CRED_GUARD_KNOWN_FAKE_FILE). Never add a real value.
# Prints PATH, shape and sha256[:12] only. Never a value, never file content.
# COP-706 follow-up (2026-10-13): the allowlist summary counts OCCURRENCES, not rows or files. A placeholder quoted in
# many transcripts / .stversions copies / memory rows is one hash and many occurrences ("21 occurrence(s) of 6
# allowlisted hash(es)" is the normal state, not 21 distinct secrets). One indented line per allowlisted hash follows
# the summary: row=<line number in the known-fake file> shape sha12 occurrences files. CRED_GUARD_EXPLAIN=1 also lists
# every file (count per file); an allowlisted hit can only come from a listed row, and rows that matched nothing are named.
# COP-911 (2026-11-26): false-positive root cause was path-pinned EXEMPT too narrow for the
# gstack AKIA fixture (now also in claude-plugins-official/security-guidance tests) plus two
# xox placeholders in agent-memory-synced/memory.db (one proven fake allowlisted; one real
# LastPass-backed value must be same-length redacted, never allowlisted).
# Usage: no-credential-literals-synced.sh [--synced | dir ...]      (no args = --synced)
# Exit: 0 clean | 1 literal found | 2 no directory could be scanned (inspected nothing).
# Clean-up for a hit: store the value in LastPass first, then same-length in-place overwrite
# (~/Sandbox/scripts/cop526_redact.py redact FILE) - never sed -i / perl -pi / rewrite-and-rename.
set -uo pipefail
exec /usr/bin/python3 - "$@" <<'PY'
import base64, glob, hashlib, json, mmap, os, re, sys
HOME = os.path.expanduser("~")
BOUND = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
# ---------------------------------------------------------------------------------------------------------------
# Shape table: ONE LINE PER SHAPE (a negative control deletes exactly one line). (name, mode, anchor, regex, alphabet)
# Every regex marks the secret portion with the named group (?P<s>...); only those bytes are overwritten.
#   prefix  regex matched at the anchor, token boundary required before it
#   back    anchor sits inside the token (':AA', '-usN'); start is searched up to 40 bytes back, boundary required
#   keyed   anchor is a key name (VERCEL*/TWILIO*); value adjacent to it
#   jwt     JWT whose payload "iss" is n8n
#   pem     private key block; the secret is the base64 characters of the body (escape sequences kept)
#   full    no anchor: whole-buffer search (Discord bot token; first segment must decode to a 17-20 digit snowflake)
SHAPES = [
    ("sk-ant-oauth", "prefix", rb"sk-", rb"sk-ant-o[ar]t[0-9]{2}-(?P<s>[A-Za-z0-9_\-]{80,})(?![A-Za-z0-9_\-])", 64),
    ("sk-ant", "prefix", rb"sk-", rb"sk-ant-(?!o[ar]t[0-9]{2}-)[a-z]{3}[0-9]{2}-(?P<s>[A-Za-z0-9_\-]{80,})(?![A-Za-z0-9_\-])", 64),
    ("sk-proj", "prefix", rb"sk-", rb"sk-proj-(?P<s>[A-Za-z0-9_\-]{40,})(?![A-Za-z0-9_\-])", 64),
    ("sk-svc", "prefix", rb"sk-", rb"sk-(?:svcacct|admin)-(?P<s>[A-Za-z0-9_\-]{40,})(?![A-Za-z0-9_\-])", 64),
    ("sk-or", "prefix", rb"sk-", rb"sk-or-v1-(?P<s>[0-9a-f]{64})(?![A-Za-z0-9_\-])", 16),
    ("sk", "prefix", rb"sk-", rb"sk-(?P<s>[A-Za-z0-9]{40,})(?![A-Za-z0-9_\-])", 64),
    ("sk-hex32", "prefix", rb"sk-", rb"sk-(?P<s>[0-9a-f]{32})(?![A-Za-z0-9_\-])", 16),
    ("glpat", "prefix", rb"glpat-", rb"glpat-(?P<s>[A-Za-z0-9_\-]{20,}(?:\.[0-9a-z]{2}\.[0-9a-z]{6,})?)(?![A-Za-z0-9_\-])", 64),
    ("aiza", "prefix", rb"AIza", rb"AIza(?P<s>[0-9A-Za-z_\-]{35})(?![0-9A-Za-z_\-])", 64),
    ("akia", "prefix", rb"AKIA", rb"AKIA(?P<s>[0-9A-Z]{16})(?![0-9A-Za-z_\-])", 36),
    ("xox", "prefix", rb"xox[abprs]-", rb"xox[abprs]-(?P<s>[0-9A-Za-z\-]{10,})(?![0-9A-Za-z_\-])", 64),
    ("github", "prefix", rb"gh[pousr]_", rb"gh[pousr]_(?P<s>[A-Za-z0-9]{36})(?![A-Za-z0-9_])", 64),
    ("github-pat", "prefix", rb"github_pat_", rb"github_pat_(?P<s>[A-Za-z0-9_]{50,})(?![A-Za-z0-9_])", 64),
    ("stripe-live", "prefix", rb"[sr]k_live_", rb"[sr]k_live_(?P<s>[0-9A-Za-z]{24,})(?![0-9A-Za-z_\-])", 62),
    ("stripe-whsec", "prefix", rb"whsec_", rb"whsec_(?P<s>[0-9A-Za-z]{32,})(?![0-9A-Za-z_\-])", 62),
    ("telegram-bot", "back", rb":AA", rb"[0-9]{8,10}:(?P<s>AA[0-9A-Za-z_\-]{33})(?![0-9A-Za-z_\-])", 64),
    ("discord-bot", "full", b"", rb"[MNO][A-Za-z0-9_\-]{21,27}\.(?P<s>[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,40})(?![A-Za-z0-9_\-.])", 64),
    ("figma", "prefix", rb"figd_", rb"figd_(?P<s>[0-9A-Za-z_\-]{40,})(?![0-9A-Za-z_\-])", 64),
    ("vercel", "prefix", rb"vc[piark]_", rb"vc[piark]_(?P<s>[0-9A-Za-z]{24,})(?![0-9A-Za-z_\-])", 62),
    ("vercel-legacy", "keyed", rb"VERCEL|vercel|Vercel", rb"(?:VERCEL|vercel|Vercel)[A-Za-z0-9_]*(?:TOKEN|Token|token|KEY|Key|key|SECRET|Secret|secret)\\{0,3}[\"']?[ \t]*[:=][ \t]*\\{0,3}[\"']?(?P<s>[0-9A-Za-z]{24})(?![0-9A-Za-z_\-])", 62),
    ("resend", "prefix", rb"re_", rb"re_(?P<s>[0-9A-Za-z_]{30,})(?![0-9A-Za-z_\-])", 64),
    ("twilio-sk", "prefix", rb"SK[0-9a-f]", rb"SK(?P<s>[0-9a-f]{32})(?![0-9A-Za-z_\-])", 16),
    ("twilio-ac", "prefix", rb"AC[0-9a-f]", rb"AC(?P<s>[0-9a-f]{32})(?![0-9A-Za-z_\-])", 16),
    ("twilio-secret", "keyed", rb"TWILIO|twilio|Twilio", rb"(?:TWILIO|twilio|Twilio)[A-Za-z0-9_]*(?:TOKEN|Token|token|SECRET|Secret|secret)\\{0,3}[\"']?[ \t]*[:=][ \t]*\\{0,3}[\"']?(?P<s>[0-9A-Za-z]{32})(?![0-9A-Za-z_\-])", 62),
    ("mailchimp", "back", rb"-us[0-9]", rb"(?P<s>[0-9a-f]{32})-us[0-9]{1,2}(?![0-9A-Za-z_\-])", 16),
    ("n8n-jwt", "jwt", rb"eyJ", rb"eyJ(?P<s>[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{20,})(?![A-Za-z0-9_\-])", 64),
    ("posthog", "prefix", rb"phx_", rb"phx_(?P<s>[0-9A-Za-z]{30,})(?![0-9A-Za-z_\-])", 62),
    ("linear", "prefix", rb"lin_api_", rb"lin_api_(?P<s>[0-9A-Za-z]{32,})(?![0-9A-Za-z_\-])", 62),
    ("notion", "prefix", rb"ntn_", rb"ntn_(?P<s>[0-9A-Za-z]{40,})(?![0-9A-Za-z_\-])", 62),
    ("notion-legacy", "prefix", rb"secret_", rb"secret_(?P<s>[0-9A-Za-z]{43})(?![0-9A-Za-z_\-])", 62),
    ("private-key", "pem", rb"-----BEGIN ", rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----(?P<s>(?:[A-Za-z0-9+/=\s]|\\){40,12000})(?:-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----)?", 64),
]
NEW_IN_COP526 = ["sk-ant-oauth", "sk-hex32", "stripe-live", "stripe-whsec", "telegram-bot", "discord-bot", "figma", "vercel", "vercel-legacy",
                 "resend", "twilio-sk", "twilio-ac", "twilio-secret", "mailchimp", "n8n-jwt", "posthog", "linear", "notion", "notion-legacy", "private-key"]
_ANCHORS = []
for _n, _m, _a, _r, _al in SHAPES:
    if _m != "full" and _a not in _ANCHORS:
        _ANCHORS.append(_a)
def _literals(anchor):
    """Expand an anchor into plain byte literals: a|b alternatives and inner [xyz] classes are expanded, a trailing
    class is dropped (the anchor regex re-checks it). Literal searches keep the scan within ~2-3x of COP-522's speed."""
    out = []
    for alt in anchor.split(b"|"):
        alt = alt.replace(b"\\-", b"-")
        if alt.endswith(b"]"):
            alt = alt[:alt.rindex(b"[")]
        parts = [b""]
        i = 0
        while i < len(alt):
            if alt[i:i + 1] == b"[":
                j = alt.index(b"]", i)
                parts = [p + bytes([c]) for p in parts for c in alt[i + 1:j]]
                i = j + 1
            else:
                parts = [p + alt[i:i + 1] for p in parts]; i += 1
        out.extend(parts)
    for lit in out:
        assert lit and not any(ch in lit for ch in b"[]()?*+{}\\"), lit
    return out


_ANCHOR_RX = [(re.compile(b"(?:%s)" % a), [(n, m, re.compile(r), al) for n, m, aa, r, al in SHAPES if aa == a and m != "full"]) for a in _ANCHORS]
_LIT = {}  # literal -> [(anchor_rx, shapes)]
for (_arx, _shapes), _a in zip(_ANCHOR_RX, _ANCHORS):
    for _l in _literals(_a):
        _LIT.setdefault(_l, []).append((_arx, _shapes))
_LIT_RX = [(re.compile(re.escape(l)), l) for l in _LIT]


def _anchor_hits(d):
    """Sorted (pos, shapes) for every anchor occurrence; shapes are those whose anchor regex matches at pos."""
    pos = []
    for rx, l in _LIT_RX:
        for m in rx.finditer(d):
            pos.append((m.start(), l))
    pos.sort()
    for p, l in pos:
        shapes = []
        for arx, sh in _LIT[l]:
            if arx.match(d, p):
                shapes.extend(sh)
        if shapes:
            yield p, shapes


_FULL = [(n, m, re.compile(r), al) for n, m, aa, r, al in SHAPES if m == "full"]
SHAPE_NAMES = [n for n, *_ in SHAPES] + ["aws-secret"]
ALPHA = {n: al for n, _m, _a, _r, al in SHAPES}
AWS_SEC = re.compile(rb"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
B64CH = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
LONG_MARKERS = [b"example", b"placeholder", b"redacted", b"changeme", b"notreal", b"replaceme", b"replace_me", b"your_", b"your-"]
SHORT_MARKERS = [b"fake", b"your", b"mock", b"dummy", b"test", b"sample", b"xxxx", b"abcdef", b"012345", b"123456"]
RUN8 = re.compile(rb"(.)\1{7}")
PRESIGNED = re.compile(rb"(x-amz-credential=|awsaccesskeyid=|credential=)[\"']?$")
# Non-credentials pinned by BOTH sha256[:12] AND file location. A copy of the same literal anywhere else
# (e.g. a transcript) is still treated as real and redacted.
_GSTACK = r"/gstack/(variants/[^/]+/)?test/brain-sync\.test(~[0-9-]+)?\.ts$"
_GSTACK_CSO = r"/gstack/(variants/[^/]+/)?test/skill-e2e-cso\.test(~[0-9-]+)?\.ts$"
_FC_SKILL = r"/Agents/fleet-capabilities/(\.stversions/)?tests/(__pycache__/)?test_fleet_skill[~.]"
_FC_CAPS = r"/Agents/fleet-capabilities/(\.stversions/)?tests/(__pycache__/)?test_fleet_capabilities[~.]"
_DEVTOOLS = r"/\.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/"
# COP-911: same AKIA demo fixture shipped in security-guidance plugin tests (path-pin; allowlist also covers copies)
_SEC_GUIDANCE = r"/claude-plugins-official/security-guidance/[^/]+/tests/test_patterns_file_shape\.py$"
EXEMPT = [
    ("457643f44d19", "akia", _GSTACK, "upstream gstack brain-sync secret-scrub test fixture"),
    ("457643f44d19", "akia", _SEC_GUIDANCE, "claude-plugins-official security-guidance demo AKIA fixture (COP-911)"),
    ("d0508aa1197f", "github", _GSTACK, "upstream gstack brain-sync secret-scrub test fixture"),
    ("2dfacb4231b3", "sk-hex32", _GSTACK_CSO, "upstream gstack /cso e2e test planted fake key (not in any env or LastPass)"),
    ("dfd56b3d10b5", "akia", _FC_SKILL, "fleet-capabilities secret-gate test fixture (COP-518 lane)"),
    ("f1c654037235", "akia", _FC_CAPS, "fleet-capabilities secret-gate test fixture"),
    ("82522273c21b", "sk", _FC_CAPS, "fleet-capabilities secret-gate test fixture"),
    ("0689c9502a0e", "aiza", _DEVTOOLS, "vendor public key in Chromium DevTools source (.gitallowed)"),
    ("431da6019dba", "aiza", _DEVTOOLS, "vendor public CrUX key in Chromium DevTools source"),
    ("e50a1cdcfa5f", "aiza", _DEVTOOLS, "vendor public key in chrome-devtools-mcp source"),
]
_EXEMPT_RX = [(h, sh, re.compile(rx)) for h, sh, rx, _ in EXEMPT]


def hits(d, path, disabled=()):
    """find() plus location-pinned exemptions. Use this, not find(), wherever a path is known."""
    for h in find(d, disabled):
        if h["cls"] is None and _EXEMPT_RX:
            sha12 = hashlib.sha256(d[h["start"]:h["end"]]).hexdigest()[:12]
            for hh, sh, rx in _EXEMPT_RX:
                if sha12 == hh and h["shape"] == sh and rx.search(path):
                    h["cls"] = "pinned-fixture"; break
        yield h


def placeholder(body, alpha=64):
    """None = real-shaped. Otherwise the placeholder class."""
    if not body or set(body) <= set(b"Xx*.0_-"):
        return "already-x"
    low = body.lower()
    if any(w in low for w in LONG_MARKERS):
        return "marker"
    if RUN8.search(body):
        return "repeat"
    n, L = len(set(body)), len(body)
    a = 16 if (alpha > 16 and all(c in b"0123456789abcdef" for c in body)) else alpha  # hex value under a wider-alphabet shape
    expect = a * (1 - ((a - 1) / a) ** L)  # expected distinct chars of a random body
    if n < max(5, 0.5 * expect):
        return "low-variety"
    if any(w in low for w in SHORT_MARKERS) and n < 0.6 * expect:
        return "marker"
    return None


def left_ok(d, pos):
    if pos == 0 or d[pos - 1] not in BOUND:
        return True
    if pos >= 2 and d[pos - 2] == 0x5C and d[pos - 1] in b"ntr":  # JSON escape \n \t \r
        return True
    if pos >= 6 and d[pos - 6] == 0x5C and d[pos - 5:pos - 2] in (b"u00", b"U00"):  # \u00XX escape
        return True
    return False


def _pem_spans(d, a, b):
    """base64 characters of a PEM body, skipping \\n / \\\\n escape sequences and whitespace."""
    spans, i, run = [], a, None
    while i < b:
        c = d[i]
        if c == 0x5C:
            j = i
            while j < b and d[j] == 0x5C:
                j += 1
            if run is not None:
                spans.append((run, i)); run = None
            i = j + 1 if (j < b and d[j] in b"nrt") else j
            continue
        if c in B64CH:
            if run is None:
                run = i
        elif run is not None:
            spans.append((run, i)); run = None
        i += 1
    if run is not None:
        spans.append((run, b))
    return spans


def _jwt_iss(tok):
    try:
        p = tok.split(b".")[1]
        return json.loads(base64.urlsafe_b64decode(p + b"=" * (-len(p) % 4))).get("iss")
    except Exception:
        return None


def _snowflake(seg):
    try:
        v = base64.urlsafe_b64decode(seg + b"=" * (-len(seg) % 4))
        return v.isdigit() and 17 <= len(v) <= 20
    except Exception:
        return False


def _hit(name, m, alpha, cls=None, spans=None):
    s, e = m.start("s"), m.end("s")
    body = m.group("s")
    return {"shape": name, "start": m.start(), "end": m.end(), "bs": s, "spans": spans or [(s, e)],
            "cls": cls if cls is not None else placeholder(bytes(body), alpha)}


def find(d, disabled=()):
    """Yield dicts: shape, start, end, bs, spans, cls (None=real). Never returns bytes of the value."""
    last_end = -1; covered = []
    for pos, shapes in _anchor_hits(d):
        if pos < last_end:
            continue
        for name, mode, rx, alpha in shapes:
            m = None
            if mode in ("prefix", "jwt", "pem"):
                if not left_ok(d, pos):
                    continue
                m = rx.match(d, pos)
            elif mode == "keyed":
                m = rx.match(d, pos)
            elif mode == "back":
                for st in range(max(0, pos - 40), pos):
                    mm = rx.match(d, st)
                    if mm and mm.end() > pos and (left_ok(d, st) or d[max(0, st - 3):st] == b"bot"):
                        m = mm; break
            if not m:
                continue
            if mode == "jwt" and _jwt_iss(m.group()) != "n8n":
                last_end = m.end(); break
            last_end = m.end(); covered.append((m.start(), m.end()))
            if name in disabled:
                break
            if mode == "pem":
                a, b = m.start("s"), m.end("s")
                b64 = bytes(x for x in bytes(d[a:b]) if x in B64CH)
                cls = "short-or-example" if len(b64) < 200 else placeholder(b64.rstrip(b"="), 64)
                yield {"shape": name, "start": m.start(), "end": m.end(), "bs": a, "spans": _pem_spans(d, a, b), "cls": cls}
                break
            h = _hit(name, m, alpha)
            if name == "akia" and h["cls"] is None and PRESIGNED.search(bytes(d[max(0, pos - 80):pos]).lower()):
                h["cls"] = "presigned-url-id"  # S3 presigned URL key id (e.g. Notion file links): an identifier, not a secret
            yield h
            if name == "akia" and "aws-secret" not in disabled:
                lo = max(0, pos - 400); nl = d.rfind(b"\n", lo, pos); lo = nl + 1 if nl >= 0 else lo
                hi = min(len(d), m.end() + 400); nl = d.find(b"\n", m.end(), hi); hi = nl if nl >= 0 else hi
                win = d[lo:hi]
                for sm in AWS_SEC.finditer(win):
                    s, e = lo + sm.start(), lo + sm.end()
                    if pos <= s < m.end():
                        continue
                    v = sm.group()
                    if not (re.search(rb"[A-Z]", v) and re.search(rb"[a-z]", v) and re.search(rb"[0-9]", v)):
                        continue
                    if b"//" in v or b"AKIA" in v:
                        continue
                    before = d[max(0, s - 100):s].lower()
                    near_after = 0 <= s - m.end() <= 120
                    if b"secret" not in before and not near_after:
                        continue
                    yield {"shape": "aws-secret", "start": s, "end": e, "bs": s, "spans": [(s, e)], "cls": placeholder(v, 64)}
            break
    for name, mode, rx, alpha in _FULL:
        if name in disabled:
            continue
        for m in rx.finditer(d):
            st = m.start()
            if not left_ok(d, st) or (st > 0 and d[st - 1] == 0x2E):
                continue
            if not _snowflake(bytes(d[st:m.start("s") - 1])):
                continue
            if any(a < m.end() and st < b for a, b in covered):
                continue
            yield _hit(name, m, alpha)




def synced_dirs():
    H = HOME
    d = [H + "/Agents", H + "/Vaults", H + "/.hermes/skills", H + "/.claude", H + "/.agent-vault", 
         H + "/.claude/plugins/data/agent-memory-synced"]
    d += glob.glob(H + "/.hermes/profiles/*/skills")
    d += [H + "/." + x + "/skills" for x in ("codex", "cursor", "agents", "openclaw", "gemini", "grok")]
    try:
        import xml.etree.ElementTree as ET
        for f in ET.parse(H + "/Library/Application Support/Syncthing/config.xml").getroot().findall("folder"):
            p = os.path.expanduser(f.get("path") or "")
            if not p:
                continue
            d.append(p)
            v = f.find("versioning"); fs = (v.findtext("fsPath") if v is not None else "") or ""
            if fs:
                d.append(fs if os.path.isabs(fs) else os.path.join(p, fs))
    except Exception:
        pass
    out = []
    for x in d:
        if x not in out:
            out.append(x)
    return out


def canonical(p):
    b = os.path.basename(p)
    if "/.stversions/" in p:
        return False
    return b == ".env" or b.startswith(".env.") or b.endswith(".env") or p == HOME + "/.claude/.credentials.json"


# COP-552 known-fake allowlist (Colby-approved 2026-09-16): FULL sha256 + shape of a value PROVEN fake (not in LastPass,
# not in any env file, source fixture located). Data lives in a DIFFERENT file from this lock so a row deletion is a
# real negative control. Rows that are not "<64 hex>\t<shape>\t<reason>" are ignored and counted; a missing file means
# no allowlist (stricter). A hash absent from the file still goes RED. Evidence: ~/Archive/cop-552-2026-09-17/.
KNOWN_FAKE_FILE = os.environ.get("CRED_GUARD_KNOWN_FAKE_FILE") or HOME + "/.fleet/credential-literal-guard/known-fake-hashes.tsv"
KNOWN_FAKE = {}; _kf_bad = 0   # (sha256, shape) -> 1-based line number of its row
try:
    for _no, _ln in enumerate(open(KNOWN_FAKE_FILE, encoding="utf-8"), 1):
        if not _ln.strip() or _ln.startswith("#"):
            continue
        _c = _ln.rstrip("\n").split("\t")
        if len(_c) >= 3 and re.fullmatch(r"[0-9a-f]{64}", _c[0]) and _c[1] in SHAPE_NAMES and _c[2].strip():
            KNOWN_FAKE.setdefault((_c[0], _c[1]), _no)
        else:
            _kf_bad += 1
except FileNotFoundError:
    pass
args = sys.argv[1:]
dirs = synced_dirs() if (not args or args == ["--synced"]) else args
scanned = 0; fails = []; seen = set(); nfiles = 0; allowed = 0
allow_hits = {}   # (sha256, shape) -> {path: occurrences}; only keys of KNOWN_FAKE can ever land here
for r in dirs:
    if not os.path.isdir(r):
        continue
    scanned += 1
    for base, ds, fs in os.walk(r, followlinks=False):
        for n in fs:
            p = os.path.join(base, n)
            try:
                s = os.lstat(p)
            except OSError:
                continue
            if (s.st_mode & 0o170000) != 0o100000 or (s.st_dev, s.st_ino) in seen or s.st_size == 0 or canonical(p):
                continue
            seen.add((s.st_dev, s.st_ino))
            try:
                fh = open(p, "rb"); mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                continue
            nfiles += 1
            try:
                for h in hits(mm, p):
                    if h["cls"] is None:
                        _full = hashlib.sha256(mm[h["start"]:h["end"]]).hexdigest()
                        if (_full, h["shape"]) in KNOWN_FAKE:
                            allowed += 1
                            _per = allow_hits.setdefault((_full, h["shape"]), {})
                            _per[p] = _per.get(p, 0) + 1
                            continue
                        fails.append((p, h["shape"], _full[:12]))
            finally:
                mm.close(); fh.close()
print("known-fake allowlist: %d row(s) loaded, %d malformed row(s) ignored, %d occurrence(s) of %d allowlisted hash(es) (%s)" % (len(KNOWN_FAKE), _kf_bad, allowed, len(allow_hits), KNOWN_FAKE_FILE))
_clean = lambda t: "".join(c if c.isprintable() else "?" for c in t)
for (_hh, _sh), _per in sorted(allow_hits.items(), key=lambda kv: (-sum(kv[1].values()), kv[0][1], kv[0][0])):
    print("  allowlisted row=%d shape=%s sha12=%s occurrences=%d files=%d" % (KNOWN_FAKE[(_hh, _sh)], _sh, _hh[:12], sum(_per.values()), len(_per)))
    if os.environ.get("CRED_GUARD_EXPLAIN") == "1":
        for _fp, _n in sorted(_per.items()):
            print("    in %s count=%d" % (_clean(_fp), _n))
_idle = sorted((_no, _sh) for (_hh, _sh), _no in KNOWN_FAKE.items() if (_hh, _sh) not in allow_hits)
if _idle:
    print("  allowlist rows with no hit in this scan: " + ", ".join("row=%d shape=%s" % x for x in _idle))
if scanned == 0:
    print("ERROR: none of the directories exist; inspected nothing: " + " ".join(dirs), file=sys.stderr); sys.exit(2)
if fails:
    agg = {}
    for p, sh, h in fails:
        agg.setdefault((p, sh), set()).add(h)
    for (p, sh), hs in sorted(agg.items()):
        print("FAIL credential literal in: %s shape=%s count=%d sha12=%s" % (p, sh, sum(1 for f in fails if f[0] == p and f[1] == sh), ",".join(sorted(hs))))
    print("RED: %d credential literal(s) in %d file(s) across %d scanned dir(s) (%d files read). Store in LastPass, then same-length redact." % (len(fails), len({f[0] for f in fails}), scanned, nfiles))
    sys.exit(1)
print("GREEN: no credential literal in %d scanned dir(s) (%d files read)" % (scanned, nfiles))
sys.exit(0)
PY
