"""JSON-LD product-price extraction — deterministic, no LLM, no selectors.

Every major retailer embeds schema.org structured data in
``<script type="application/ld+json">`` blocks because Google rich results
require it — so the price sits in a *standardized* machine-readable contract
(``Offer.price`` / ``Offer.priceCurrency`` / ``Offer.availability``), not in
per-site CSS. Verified live 2026-08-23 through the argus dev server:

- pbtech.co.nz PDP: ``"price":499`` (int), NZD, InStock.
- kogan.com.nz PDP: ``"price":"264"`` (str), NZD, InStock.
- farmers.co.nz PDP: ``"price":"599.99"`` (str), NZD, InStock, plus a
  non-standard ``"original_price":"1199.99"`` riding in the Product node.

One recursive walker covers all three with zero per-site branching (the
quality-guidelines forbidden pattern). The walker tolerates the real-world
mess: prices as int or str, thousands separators (``"1,199.99"``), offers
nested in ``Product`` nodes vs standalone, top-level ``@graph`` arrays, and
multiple ld+json blocks where only one carries the Offer.

Sanity guard: a price is *usable* only when it parses as a finite Decimal
``> 0`` after stripping separators — placeholder ``0``/negative values are
skipped rather than trusted (stale-placeholder prices would otherwise masquerade
as real data). Non-standard extras like farmers' ``original_price`` need no
special handling: they ride along inside the returned Product node.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Optional

logger = logging.getLogger("argus.jsonld")

# Attribute order/casing varies across sites (type='application/ld+json' vs
# type="application/ld+json;charset=utf-8"), hence the loose match.
_LD_SCRIPT_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

# Typed PriceSpecification nodes (shipping/unit/delivery/payment charges) are
# excluded from candidacy structurally: _iter_offer_candidates yields only
# Offer/AggregateOffer-typed nodes or UNTYPED dicts carrying both ``price``
# and ``priceCurrency`` — a typed PriceSpecification is neither.

# Availability URIs that mean "cannot buy it right now". Everything else
# (InStock, LimitedAvailability, PreOrder, PreSale, InStoreOnly, OnlineOnly,
# unknown/absent) counts as available — absence of the field with a positive
# price present is treated as purchasable, matching how retailers ship the
# field in practice. SoldOut is included alongside OutOfStock: schema.org
# defines both, and retailers use them interchangeably to mean unsellable.
_UNAVAILABLE_AVAILABILITY = frozenset({"outofstock", "soldout", "discontinued"})


@dataclass
class OfferExtract:
    """A usable price found in a page's JSON-LD."""

    # Decimal-normalized string with 2dp (e.g. "499.00", "599.99") — string,
    # not float, so JSON round-trips without binary rounding.
    price: str
    currency: Optional[str]
    availability: Optional[str]
    name: Optional[str]
    # The enclosing schema.org Product node (name/brand/gtin/reviews ride
    # along) — the caller-facing "rich data" payload. Falls back to the Offer
    # node itself when the offer is not nested in a Product.
    product_node: dict


def available_from(availability: Optional[str]) -> bool:
    """Map a schema.org ItemAvailability URI to a purchasable verdict."""
    if availability is None:
        return True
    tail = availability.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail not in _UNAVAILABLE_AVAILABILITY


def _parse_blocks(html: str) -> Iterator[Any]:
    """Yield parsed ld+json payloads, tolerating concatenated objects.

    Some sites emit several ld+json objects back-to-back inside ONE script tag
    without a separating comma (``}{``). Direct parsing fails there; inserting
    commas and wrapping in a list recovers them. Unrecoverable blocks are
    skipped — one broken script must not sink the whole extraction.
    """
    for raw in _LD_SCRIPT_RE.findall(html):
        blob = raw.strip()
        if not blob:
            continue
        try:
            yield json.loads(blob)
            continue
        except json.JSONDecodeError:
            pass
        comma_joined = re.sub(r"}\s*{", "},{", blob)
        for candidate in (comma_joined, "[" + comma_joined + "]"):
            try:
                yield json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue
        else:
            logger.debug("skipping unparseable ld+json block len=%d", len(blob))


def _node_types(node: dict) -> set[str]:
    """String @type values of a node (schema.org allows str or list)."""
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _iter_offer_candidates(
    node: Any, parent_product: Optional[dict] = None
) -> Iterator[tuple[dict, Optional[dict]]]:
    """Yield ``(offer_node, enclosing_product_or_None)`` in document order.

    Candidates are Offer/AggregateOffer typed nodes, or untyped dicts carrying
    both ``price`` and ``priceCurrency`` (some sites omit the type). Once a
    candidate yields, its subtree is not descended further — price blobs nested
    inside an offer (e.g. PriceSpecification) must not resurface as competing
    candidates. ``parent_product`` threads the nearest Product ancestor down
    the walk so the rich-data node rides home with the price.
    """
    if isinstance(node, dict):
        types = _node_types(node)
        current_product = node if "Product" in types else parent_product

        is_offer_typed = bool(types & {"Offer", "AggregateOffer"})
        is_anonymous_offer = (
            not types
            and "price" in node
            and "priceCurrency" in node
        )
        if is_offer_typed or is_anonymous_offer:
            yield node, current_product
            return

        for value in node.values():
            yield from _iter_offer_candidates(value, current_product)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_offer_candidates(item, parent_product)


def _normalize_price(value: Any) -> Optional[Decimal]:
    """Decimal with 2dp for a usable positive price, else None.

    Accepts int (pbtech ``499``), str (kogan ``"264"``, farmers
    ``"599.99"``), and thousands-separated strings (``"1,199.99"``). Rejects
    placeholders (``0``, negatives), currency-prefixed garbage, booleans, and
    non-finite values — a price we cannot trust is a price we skip.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", ""))
        if not parsed.is_finite() or parsed <= 0:
            return None
        return parsed.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _candidate_price(candidate: dict) -> Optional[Decimal]:
    """Usable price for one candidate node, honoring the aggregate shape."""
    types = _node_types(candidate)
    if "AggregateOffer" in types:
        # lowPrice = cheapest offering in the range — the "from $X" price and
        # the closest analogue to a single-Offer price. highPrice deliberately
        # unused (range top is not a selling price).
        for key in ("lowPrice",):
            if key in candidate:
                return _normalize_price(candidate[key])
        return None
    if "price" in candidate:
        return _normalize_price(candidate["price"])
    return None


def extract_offer(html: str) -> Optional[OfferExtract]:
    """First usable JSON-LD price on the page, or None.

    "First usable" walks candidates in document order and returns the first
    whose price normalizes to a positive Decimal — a broken/placeholder
    candidate does not abort the search, it just loses to the next one.
    """
    for block in _parse_blocks(html):
        for candidate, product in _iter_offer_candidates(block):
            price = _candidate_price(candidate)
            if price is None:
                continue
            name: Optional[str] = None
            if product is not None:
                raw_name = product.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name = raw_name
            if name is None:
                raw_name = candidate.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name = raw_name
            return OfferExtract(
                price=str(price),
                currency=candidate.get("priceCurrency"),
                availability=candidate.get("availability"),
                name=name,
                product_node=product if product is not None else candidate,
            )
    return None
