"""Secret inputs: values that must never reach reports, saved files, routes, chat output or the decision model.

A secret is declared as `inputs: {pw: {env: APP_PASSWORD}}` (value read from the environment / .env at run time,
so it is never in the YAML) - literal secrets in YAML are rejected. Everything that leaves the runtime goes through
Masker.mask(): the model-bound screen text, step details, oracle notes, evidence text, the report and its JSON.
Screenshots cannot be redacted; reports say so when secrets are in play.
"""
import urllib.parse

MASK = "•••"
SECRETISH = ("password", "passwd", "pass", "token", "secret", "apikey", "api_key", "credential", "pin")


class Masker:
    def __init__(self, values=()):
        forms = set()
        for v in values:
            v = str(v)
            if not v:
                continue
            forms.update({v, urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v), v.replace(" ", "%s")})
        # longest first so a secret that contains another secret is masked whole
        self.forms = sorted((f for f in forms if f), key=len, reverse=True)

    def mask(self, text):
        if not isinstance(text, str):
            return text
        for f in self.forms:
            text = text.replace(f, MASK)
        return text

    def __bool__(self):
        return bool(self.forms)


def looks_secret(name):
    n = name.lower()
    return any(k in n for k in SECRETISH)
