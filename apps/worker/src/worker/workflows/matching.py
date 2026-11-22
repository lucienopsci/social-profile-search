"""Vérification de pertinence: règles exact / fuzzy / approximative de la skill.

- match exact (account_name == search_query, casse et accents insensibles) -> MUST TRUE
- match fuzzy (username proche de la requête) -> TRUE
- match approximatif (nom partiel ET description partageant des éléments de l'input) -> TRUE
- sinon FALSE
"""
import re
import unicodedata

from .models import TargetInput


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s)


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2}


def _name_tokens(name: str) -> str:
    return " ".join(sorted(_tokens(name)))


def is_exact_match(account_name: str, query: str) -> bool:
    return _norm(account_name) == _norm(query)


def is_fuzzy_match(account_username: str, query: str) -> bool:
    nu, nq = _norm(account_username), _norm(query)
    return bool(nu) and (nu == nq or (len(nu) >= 4 and (nu in nq or nq in nu)))


def description_overlap(account_description: str, input_description: str) -> bool:
    """Partage des éléments entre la bio du compte et la description de l'input."""
    a, b = _tokens(account_description), _tokens(input_description)
    return len(a & b) >= 1 if b else False


def partial_name_match(account_name: str, account_username: str, query: str) -> bool:
    name = _norm(account_name) or _norm(account_username)
    q = _norm(query)
    return bool(name) and bool(q) and (
        name in q or q in name
        or any(pt in name for pt in _tokens(query))
    )


def is_relevant(account: dict, target: TargetInput) -> bool:
    """Retourne TRUE/FALSE pour la colonne relevant d'un compte trouvé."""
    q = target.name
    if is_exact_match(account.get("account_name", ""), q):
        return True  # exact match obligatoirement TRUE, même en cas d'homonymie
    if is_fuzzy_match(account.get("account_username", ""), q):
        return True
    if partial_name_match(
        account.get("account_name", ""), account.get("account_username", ""), q
    ) and description_overlap(
        account.get("account_description", ""), target.description
    ):
        return True
    return False