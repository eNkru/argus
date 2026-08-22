"""JSON-LD price extraction — pure module, offline.

Fixtures mirror the real shapes verified live on 2026-08-23: pbtech (int
price), kogan (str price), farmers (str price + non-standard original_price),
plus the structural mess the walker must tolerate.
"""

from __future__ import annotations

from argus.jsonld import available_from, extract_offer


def _page(*scripts: str) -> str:
    return "<html><head>" + "".join(scripts) + "</head><body>x</body></html>"


def _ld(payload: str) -> str:
    return f'<script type="application/ld+json">{payload}</script>'


# --- the three live-verified retailers ---------------------------------------

def test_pbtech_int_price_nested_in_product() -> None:
    html = _page(
        _ld(
            '{"@context":"https://schema.org","@type":"Product",'
            '"name":"LG UltraGear 32G620B-B 32\\" QHD Monitor",'
            '"brand":{"@type":"Brand","name":"LG"},'
            '"offers":{"@type":"Offer","price":499,"priceCurrency":"NZD",'
            '"availability":"https://schema.org/InStock",'
            '"itemCondition":"https://schema.org/NewCondition"}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "499.00"  # int normalized to 2dp Decimal string
    assert result.currency == "NZD"
    assert result.availability == "https://schema.org/InStock"
    assert available_from(result.availability) is True
    assert result.name == 'LG UltraGear 32G620B-B 32" QHD Monitor'
    # Rich data rides home: the whole Product node is returned.
    assert result.product_node["brand"]["name"] == "LG"


def test_kogan_str_price() -> None:
    html = _page(
        _ld(
            '{"@type":"Product","name":"Kogan SmarterHome LX16 Pro",'
            '"offers":{"@type":"Offer","url":"https://www.kogan.com/nz/buy/x/",'
            '"priceCurrency":"NZD","price":"264",'
            '"availability":"https://schema.org/InStock"}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "264.00"
    assert result.currency == "NZD"


def test_farmers_str_price_with_original_price_rides_in_node() -> None:
    # farmers' non-standard "original_price" needs no special handling — it is
    # part of the Product node and surfaces in jsonld untouched.
    html = _page(
        _ld(
            '{"@type":"Product","name":"Breville Barista Express BES870BKS",'
            '"sku":"6676084",'
            '"offers":{"@type":"Offer","price":"599.99","priceCurrency":"NZD",'
            '"original_price":"1199.99",'
            '"availability":"https://schema.org/InStock"}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "599.99"
    assert result.product_node["sku"] == "6676084"
    assert result.product_node["offers"]["original_price"] == "1199.99"


# --- normalization edge cases -------------------------------------------------

def test_thousands_separator_price() -> None:
    html = _page(_ld('{"@type":"Offer","price":"1,199.99","priceCurrency":"NZD"}'))
    result = extract_offer(html)
    assert result is not None
    assert result.price == "1199.99"


def test_placeholder_and_garbage_prices_are_skipped_not_fatal() -> None:
    # A placeholder offer loses to a later valid one; garbage never crashes.
    html = _page(
        _ld('{"@type":"Offer","price":0,"priceCurrency":"NZD"}'),
        _ld('{"@type":"Offer","price":-5,"priceCurrency":"NZD"}'),
        _ld('{"@type":"Offer","price":"$abc","priceCurrency":"NZD"}'),
        _ld('{"@type":"Offer","price":42.5,"priceCurrency":"NZD"}'),
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "42.50"


def test_no_jsonld_returns_none() -> None:
    assert extract_offer("<html><body>just text</body></html>") is None
    assert extract_offer("") is None


def test_unparseable_block_skipped_valid_block_still_found() -> None:
    html = _page(
        "<script type=\"application/ld+json\">{not json at all</script>",
        _ld('{"@type":"Offer","price":"9.90","priceCurrency":"NZD"}'),
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "9.90"


def test_concatenated_objects_without_separator_parse() -> None:
    # Some sites emit several ld+json objects back-to-back in ONE script tag.
    blob = (
        '{"@type":"WebSite","name":"Shop"}'
        '{"@type":"Product","name":"Widget",'
        '"offers":{"@type":"Offer","price":"12.34","priceCurrency":"NZD"}}'
    )
    result = extract_offer(_page(_ld(blob)))
    assert result is not None
    assert result.price == "12.34"
    assert result.name == "Widget"


# --- structural variants -------------------------------------------------------

def test_multiple_blocks_only_one_carries_offer() -> None:
    html = _page(
        _ld('{"@type":"WebSite","name":"Retailer"}'),
        _ld('{"@type":"BreadcrumbList","itemListElement":[]}'),
        _ld(
            '{"@type":"Product","name":"Monitor",'
            '"offers":{"@type":"Offer","price":"899","priceCurrency":"NZD"}}'
        ),
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "899.00"
    assert result.name == "Monitor"


def test_top_level_graph_array() -> None:
    # @graph wrapping is common on larger shops; the walker recurses through it.
    html = _page(
        _ld(
            '{"@context":"https://schema.org","@graph":['
            '{"@type":"WebSite","name":"S"},'
            '{"@type":"Product","name":"G Widget",'
            '"offers":{"@type":"Offer","price":"7","priceCurrency":"NZD"}}]}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "7.00"
    assert result.name == "G Widget"


def test_aggregateoffer_uses_low_price() -> None:
    html = _page(
        _ld(
            '{"@type":"Product","name":"Range Thing",'
            '"offers":{"@type":"AggregateOffer","lowPrice":"10.50",'
            '"highPrice":"99.00","priceCurrency":"NZD",'
            '"offerCount":"4"}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "10.50"  # cheapest offering, not the range top


def test_standalone_offer_without_product_wrapper() -> None:
    # No enclosing Product: the Offer node itself is the returned rich payload.
    html = _page(_ld('{"@type":"Offer","price":"19.99","priceCurrency":"NZD"}'))
    result = extract_offer(html)
    assert result is not None
    assert result.product_node["@type"] == "Offer"
    assert result.name is None


def test_anonymous_offer_node_without_type() -> None:
    html = _page(_ld('{"price":"55.00","priceCurrency":"NZD"}'))
    result = extract_offer(html)
    assert result is not None
    assert result.price == "55.00"


def test_price_specification_inside_product_is_not_the_selling_price() -> None:
    # A shipping/unit price spec carries price+priceCurrency but must not be
    # extracted when a real Offer exists — and an untyped spec alone should not
    # masquerade as one either.
    html = _page(
        _ld(
            '{"@type":"Product","name":"Thing",'
            '"offers":{"@type":"Offer","price":"30.00","priceCurrency":"NZD",'
            '"priceSpecification":{"price":"5.00","priceCurrency":"NZD"}}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.price == "30.00"

    specs_only = _page(_ld('{"@type":"PriceSpecification","price":"5.00","priceCurrency":"NZD"}'))
    assert extract_offer(specs_only) is None


def test_list_type_annotation() -> None:
    # schema.org allows @type as a list.
    html = _page(
        _ld(
            '{"@type":["Product","SomeExtension"],"name":"Listy",'
            '"offers":{"@type":["Offer"],"price":"3.20","priceCurrency":"NZD"}}'
        )
    )
    result = extract_offer(html)
    assert result is not None
    assert result.name == "Listy"
    assert result.price == "3.20"


# --- availability mapping -------------------------------------------------------

def test_available_from_uris() -> None:
    assert available_from(None) is True
    assert available_from("https://schema.org/InStock") is True
    assert available_from("https://schema.org/LimitedAvailability") is True
    assert available_from("https://schema.org/PreOrder") is True
    assert available_from("https://schema.org/OutOfStock") is False
    assert available_from("https://schema.org/SoldOut") is False
    assert available_from("https://schema.org/Discontinued") is False
    assert available_from("http://schema.org/InStock/") is True  # trailing slash
