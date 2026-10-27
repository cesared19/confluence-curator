"""Búsqueda BM25 sobre las secciones indexadas (español e inglés, sin acentos)."""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

STOPWORDS = set(
    """
    a al algo algunas algunos ante antes como con contra cual cuando de del desde donde durante e el ella ellas
    ellos en entre era es esa ese eso esta este esto estos fue ha hay la las le les lo los mas me mi muy no nos o
    otra otro para pero por porque que se sea segun ser si sin sobre su sus tambien te tiene todo tu un una uno unos
    y ya the and or of to in on for with is are be this that it as at by from an not can will should into when
    """.split()
)


def tokenize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z0-9]+", folded) if len(t) > 1 and t not in STOPWORDS]


@dataclass
class Document:
    key: str
    text: str


class BM25:
    def __init__(self, documents: list[Document], k1: float = 1.5, b: float = 0.75):
        self.documents = documents
        self.k1, self.b = k1, b
        self.terms = [Counter(tokenize(d.text)) for d in documents]
        self.lengths = [sum(t.values()) for t in self.terms]
        self.average = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0
        frequency: Counter = Counter()
        for terms in self.terms:
            frequency.update(terms.keys())
        n = len(documents)
        self.idf = {term: math.log(1 + (n - df + 0.5) / (df + 0.5)) for term, df in frequency.items()}

    def search(self, query: str, limit: int) -> list[tuple[Document, float]]:
        query_terms = set(tokenize(query))
        scored = []
        for document, terms, length in zip(self.documents, self.terms, self.lengths):
            score = 0.0
            for term in query_terms:
                tf = terms.get(term)
                if not tf:
                    continue
                norm = tf + self.k1 * (1 - self.b + self.b * length / (self.average or 1))
                score += self.idf[term] * tf * (self.k1 + 1) / norm
            if score > 0:
                scored.append((document, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]
