"""Screen-Time app → category mapping.

Categories are derived server-side (at ingest) from the app bundle id so the
client collector stays dumb and the mapping can be revised without touching the
Mac/iPhone side. Matching is a **case-insensitive substring** test against the
bundle id.

Precedence matters. Many first-party apps share the ``com.apple.*`` prefix
(Messages = com.apple.MobileSMS, Music = com.apple.Music, Safari =
com.apple.mobilesafari, …), and the spec also lists a generic ``com.apple.*``
→ System catch-all. So the specific buckets (Social / Productivity /
Entertainment / Health) are checked FIRST, and the generic Apple/System
catch-all only applies to whatever is left. `_ORDER` encodes that precedence.

Caveat worth knowing: the X/Twitter app's bundle id is ``com.atebits.Tweetie2``
— it contains neither "twitter" nor a safe "x" token ("x" as a bare substring
would also match Xcode, Dropbox, etc.), so Social matches ``twitter`` and
``tweetie`` and deliberately skips bare "x".
"""
from typing import Optional

# Ordered (category, [substrings]) pairs. First matching category wins, so the
# list order IS the precedence — specific buckets before the generic System
# catch-all. Communication is checked BEFORE Social so e.g.
# com.facebook.Messenger (contains "facebook") lands in Communication, not Social.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("AI", (                   # AI assistants / chatbots
        "anthropic", "claude", "com.openai", "chatgpt",
        "com.google.gemini", "perplexity", "copilot",
    )),
    ("Health", (
        "athlytic", "hevy", "wellness",
        # anchored tokens so "health"/"fitness" don't over-match unrelated ids
        ".health", "com.apple.health", "fitness",
    )),
    ("Communication", (        # 1:1 / small-group messaging & calling (not feeds, not email)
        "com.apple.mobilesms", "com.apple.madrid",   # Messages
        "com.apple.facetime", "com.apple.avconference",
        "com.apple.incallservice",                   # phone calls
        "messenger", "com.facebook.messenger",
        "marcopolo", "happybits",                    # Marco Polo (co.happybits.marcopolo)
        "whatsapp", "discord", "telegram",
        "org.whispersystems",                        # Signal
    )),
    ("Navigation", (           # maps / turn-by-turn
        "com.apple.maps", "com.google.maps", "waze",
    )),
    ("Social", (               # public social-media feeds
        "instagram", "tiktok", "musically",        # TikTok legacy id: com.zhiliaoapp.musically
        "twitter", "tweetie",                        # X/Twitter (com.atebits.Tweetie2)
        "snapchat", "facebook", "threads", "burbn.barcelona",  # Threads = com.burbn.barcelona
        "reddit", "linkedin", "mastodon", "bsky",
    )),
    ("Learning", (            # reading / courses / study
        "substack", "kindle", "audible", "goodreads",
        "duolingo", "anki", "coursera", "khanacademy", "brilliant",
        "wikipedia", "com.apple.ibooks", "com.apple.books",
    )),
    ("Productivity", (
        "notion", "slack", "zoom", "us.zoom", "teams",
        "com.apple.ical", "com.apple.mobilecal", "calendar",
        # Email → Productivity (Apple Mail, Gmail, ProtonMail, Outlook, Spark, Fastmail, …)
        "com.apple.mail", "com.apple.mobilemail", "gmail",
        "protonmail", "proton.me", "outlook", "readdle", "fastmail", "hey.app", "airmail",
        "microsoft.word", "microsoft.excel",
        "com.apple.iwork.pages", "com.apple.iwork.numbers",
        "com.apple.notes",
        # Browsers (Safari/Chrome/Brave/Firefox/Edge) grouped together
        "safari", "chrome", "brave", "firefox", "mozilla", "edgemobile", "microsoftedge",
        "com.apple.webapp",                           # home-screen web apps (PWAs)
    )),
    ("Entertainment", (
        "youtube", "netflix", "hulu", "spotify",
        "com.apple.music", "com.apple.podcasts",
        "com.apple.tv", "com.apple.videos",
        "games", ".game.", "game-center", "pokemon", "twitch",
    )),
    # Generic Apple / system catch-all — only reached if none of the specific
    # buckets above matched (so Maps/Notes/webapp/calls are already pulled out).
    ("System", (
        "springboard", "com.apple.springboard",
        "preferences", "com.apple.preferences", "systempreferences",
        "com.apple.",                                 # any remaining first-party app
    )),
]


def categorize_app(bundle_id: Optional[str]) -> str:
    """Map an app bundle id to one of:
    AI | Communication | Navigation | Social | Learning | Productivity |
    Entertainment | Health | System | Other.

    Case-insensitive substring match; precedence is `_RULES` order. Unknown /
    empty ids fall through to "Other".
    """
    if not bundle_id:
        return "Other"
    b = bundle_id.lower()
    for category, needles in _RULES:
        if any(n in b for n in needles):
            return category
    return "Other"
