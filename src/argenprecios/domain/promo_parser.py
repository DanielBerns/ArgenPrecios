"""Promotional legend parser and effective unit promo price calculator for SEPA and Web Scraping."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from argenprecios.domain.enums import PromoMechanism

_MULTIPACK_X_FOR_Y_RE = re.compile(r"\b(\d+)\s*[xX]\s*(\d+)\b")
_SECOND_UNIT_PCT_RE = re.compile(
    r"\b(?:2\s*(?:do|da|da\.|do\.)|segund[oa])\s*(?:unidad\s*)?(?:al\s*)?(\d{1,2})\s*%",
    re.IGNORECASE,
)
_DIRECT_PCT_OFF_RE = re.compile(r"\b(\d{1,2})\s*%\s*(?:off|dto|desc|descuento)\b", re.IGNORECASE)
_BANK_KEYWORDS_RE = re.compile(
    r"\b(?:banco|cuenta\s*dni|modo|bna|galicia|santander|bbva|macro|nacion|provincia|icbc|hsbc|tarjeta\s+de\s+credito|tarjeta\s+de\s+debito)\b",
    re.IGNORECASE,
)
_LOYALTY_KEYWORDS_RE = re.compile(
    r"\b(?:mi\s*carrefour|comunidad\s*coto|jumbo\s*prime|mas\s*club|dia\s*club|club\s*la\s*nacion|clar[ií]n\s*365|puntos|fidelidad|socio)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ParsedPromotion:
    """Normalized promotional pricing output."""

    mechanism: PromoMechanism
    promo_price_cents: int | None
    raw_legend: str | None
    discount_factor: float | None
    code_hash: str | None


def parse_ars_to_cents(raw_value: str | int | float | Decimal | None) -> int | None:
    """Parse an Argentine Peso decimal/string value into exact integer centavos."""
    if raw_value is None:
        return None
    if isinstance(raw_value, int):
        return raw_value * 100 if raw_value > 0 else None
    text_val = str(raw_value).strip()
    if not text_val:
        return None
    # Handle comma decimal separators only when no dot decimal exists (e.g. "1250,50")
    if "," in text_val and "." not in text_val:
        text_val = text_val.replace(",", ".")
    elif "," in text_val and "." in text_val:
        # e.g. "1.250,50" -> "1250.50"
        if text_val.rfind(",") > text_val.rfind("."):
            text_val = text_val.replace(".", "").replace(",", ".")
        else:
            text_val = text_val.replace(",", "")
    try:
        dec = Decimal(text_val)
    except InvalidOperation:
        return None
    if dec <= 0:
        return None
    cents = int((dec * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents if cents > 0 else None


def classify_promo_legend(legend: str | None) -> tuple[PromoMechanism, float | None]:
    """Classify a raw promotional text string into (PromoMechanism, implied_unit_factor)."""
    if not legend or not legend.strip():
        return (PromoMechanism.NONE, None)

    cleaned = " ".join(legend.strip().split())

    m_xy = _MULTIPACK_X_FOR_Y_RE.search(cleaned)
    if m_xy:
        take_n = int(m_xy.group(1))
        pay_m = int(m_xy.group(2))
        if take_n > pay_m > 0:
            factor = pay_m / take_n
            if take_n == 2 and pay_m == 1:
                return (PromoMechanism.TWO_FOR_ONE, factor)
            if take_n == 3 and pay_m == 2:
                return (PromoMechanism.THREE_FOR_TWO, factor)
            return (PromoMechanism.MULTIPACK_DISCOUNT, factor)

    m_second = _SECOND_UNIT_PCT_RE.search(cleaned)
    if m_second:
        pct = int(m_second.group(1))
        if 0 < pct < 100:
            # "2do al 70%" in Argentine retail means 70% OFF the 2nd unit -> pay 100% + 30% = 65% each
            factor = (1.0 + (1.0 - (pct / 100.0))) / 2.0
            return (PromoMechanism.SECOND_UNIT_PCT, factor)

    if _BANK_KEYWORDS_RE.search(cleaned):
        m_pct = _DIRECT_PCT_OFF_RE.search(cleaned)
        bank_factor = (1.0 - int(m_pct.group(1)) / 100.0) if m_pct else None
        return (PromoMechanism.BANK_DISCOUNT, bank_factor)

    if _LOYALTY_KEYWORDS_RE.search(cleaned):
        m_pct = _DIRECT_PCT_OFF_RE.search(cleaned)
        loyalty_factor = (1.0 - int(m_pct.group(1)) / 100.0) if m_pct else None
        return (PromoMechanism.LOYALTY_CARD, loyalty_factor)

    m_direct = _DIRECT_PCT_OFF_RE.search(cleaned)
    if m_direct:
        pct = int(m_direct.group(1))
        if 0 < pct < 100:
            return (PromoMechanism.DIRECT_DISCOUNT, 1.0 - (pct / 100.0))

    return (PromoMechanism.OTHER, None)


def evaluate_promotion(
    list_price_cents: int,
    explicit_promo_cents: int | None = None,
    raw_legend: str | None = None,
) -> ParsedPromotion:
    """Resolve effective promo_price_cents, mechanism, and deterministic rule hash."""
    cleaned_legend = " ".join(raw_legend.strip().split())[:255] if raw_legend else None
    mechanism, implied_factor = classify_promo_legend(cleaned_legend)

    effective_promo: int | None = None
    if explicit_promo_cents is not None and 0 < explicit_promo_cents < list_price_cents:
        effective_promo = explicit_promo_cents
        if mechanism == PromoMechanism.NONE:
            mechanism = PromoMechanism.DIRECT_DISCOUNT
    elif implied_factor is not None and 0.0 < implied_factor < 1.0:
        derived = round(list_price_cents * implied_factor)
        if 0 < derived < list_price_cents:
            effective_promo = derived

    if effective_promo is None and not cleaned_legend:
        return ParsedPromotion(
            mechanism=PromoMechanism.NONE,
            promo_price_cents=None,
            raw_legend=None,
            discount_factor=None,
            code_hash=None,
        )

    final_factor = (
        round(effective_promo / list_price_cents, 4)
        if effective_promo is not None and list_price_cents > 0
        else implied_factor
    )
    legend_for_hash = cleaned_legend or f"auto:{mechanism.value}:{final_factor}"
    digest = hashlib.sha256(f"{mechanism.value}|{legend_for_hash.lower()}".encode()).hexdigest()

    return ParsedPromotion(
        mechanism=mechanism if mechanism != PromoMechanism.NONE else PromoMechanism.DIRECT_DISCOUNT,
        promo_price_cents=effective_promo,
        raw_legend=legend_for_hash,
        discount_factor=final_factor,
        code_hash=digest,
    )


def evaluate_sepa_dual_promos(
    list_price_cents: int,
    promo1_raw_price: str | None,
    promo1_legend: str | None,
    promo2_raw_price: str | None,
    promo2_legend: str | None,
) -> ParsedPromotion:
    """Evaluate SEPA's promo1 and promo2 fields and select the best consumer-facing offer."""
    p1 = evaluate_promotion(
        list_price_cents=list_price_cents,
        explicit_promo_cents=parse_ars_to_cents(promo1_raw_price),
        raw_legend=promo1_legend,
    )
    p2 = evaluate_promotion(
        list_price_cents=list_price_cents,
        explicit_promo_cents=parse_ars_to_cents(promo2_raw_price),
        raw_legend=promo2_legend,
    )

    if p1.promo_price_cents is not None and p2.promo_price_cents is not None:
        return p1 if p1.promo_price_cents <= p2.promo_price_cents else p2
    if p1.promo_price_cents is not None:
        return p1
    if p2.promo_price_cents is not None:
        return p2
    if p1.code_hash is not None:
        return p1
    return p2


__all__ = [
    "ParsedPromotion",
    "classify_promo_legend",
    "evaluate_promotion",
    "evaluate_sepa_dual_promos",
    "parse_ars_to_cents",
]
