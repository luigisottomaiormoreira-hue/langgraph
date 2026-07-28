"""Formatação de números no padrão brasileiro.

`f"{v:,.2f}"` produz `3,146.29` — formato errado para quem lê o relatório. Como
`locale` depende do sistema ter pt_BR instalado (e não ter é o caso comum em
contêiner), a conversão é feita à mão.
"""

from __future__ import annotations


def num(value: float, decimals: int = 2) -> str:
    """1234567.8 -> '1.234.567,80'"""
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def brl(value: float) -> str:
    """-1234.5 -> '-R$ 1.234,50'"""
    sign = "-" if value < 0 else ""
    return f"{sign}R$ {num(abs(value))}"


def pct(value: float, decimals: int = 1) -> str:
    """0.1052 -> '10,5%'"""
    return f"{num(value * 100, decimals)}%"


def signed_pct(value: float, decimals: int = 1) -> str:
    """-0.204 -> '-20,4%'"""
    sign = "+" if value >= 0 else "-"
    return f"{sign}{num(abs(value) * 100, decimals)}%"


def times(value: float) -> str:
    """3.4567 -> '3,46x'"""
    return f"{num(value, 2)}x"
